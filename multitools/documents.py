"""Index de passages des documents sélectionnés par l’utilisateur."""

from contextlib import contextmanager
from pathlib import Path
import os
import re
import sqlite3

from .kiwix import Source, plain_html
from .local_http import LocalServiceError, check_cancel


EXTENSIONS = {".txt", ".md", ".rst", ".html", ".htm", ".text"}
MAX_FILE = 8 * 1024 * 1024
STOP_WORDS = set("avec dans sur des les une pour que qui est moi nous vous aux comment encore explique peux peut disponible utiliser utilisant selon quelle quels faire cela votre what how the and from".split())


def terms(query: str) -> list[str]:
    words = re.findall(r"[^\W_]+", query.casefold())
    useful = [word for word in words if len(word) >= 2 and word not in STOP_WORDS]
    return list(dict.fromkeys(useful or words))[:10]


def _passages(text: str):
    lines = []
    length = 0
    first = 1
    for number, line in enumerate(text.splitlines(), 1):
        for start in range(0, len(line), 1000):
            part = line[start:start + 1000].strip()
            if not part:
                continue
            if lines and length + len(part) + 1 > 1250:
                yield first, " ".join(lines)
                lines, length = [], 0
            if not lines:
                first = number
            lines.append(part)
            length += len(part) + 1
    if lines:
        yield first, " ".join(lines)


def _signature(stat):
    return f"{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}:{stat.st_ino}"


class Documents:
    def __init__(self, database: Path):
        self.database = database

    @contextmanager
    def _open(self, *, read_only=False):
        if read_only:
            con = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        else:
            self.database.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.database)
        try:
            yield con
        finally:
            con.close()

    def index(self, roots: list[Path], gate, report) -> int:
        if not roots:
            raise ValueError("Sélectionne au moins un dossier de documents.")
        with self._open() as con:
            try:
                con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
                    path UNINDEXED, line UNINDEXED, title, text, tokenize='unicode61 remove_diacritics 2')""")
            except sqlite3.OperationalError as error:
                if "fts5" in str(error).lower():
                    raise LocalServiceError("SQLite ne fournit pas FTS5 sur cet interpréteur.") from error
                raise
            con.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, signature TEXT)")
            con.commit()
            indexed = dict(con.execute("SELECT path, signature FROM files"))
            seen = set()
            count = 0
            errors = []
            for root in roots:
                gate.check()
                if not root.is_dir():
                    errors.append(str(root))
                    report(f"Dossier absent · {root}")
                    continue
                for folder, dirs, names in os.walk(root, onerror=lambda error: errors.append(str(error))):
                    gate.check()
                    dirs[:] = [name for name in dirs if not (Path(folder) / name).is_junction()]
                    for name in names:
                        path = Path(folder) / name
                        if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                            continue
                        gate.check()
                        try:
                            signature = _signature(path.stat())
                            if str(path) in seen:
                                continue
                            seen.add(str(path))
                            if indexed.get(str(path)) == signature:
                                continue
                            with path.open("rb") as stream:
                                data = stream.read(MAX_FILE + 1)
                            if len(data) > MAX_FILE:
                                continue
                            text = data.decode("utf-8-sig", "replace")
                            html = path.suffix.lower() in {".html", ".htm"}
                            if html:
                                text = plain_html(text)
                            if signature != _signature(path.stat()):
                                raise OSError("Fichier modifié pendant l’indexation.")
                            with con:
                                con.execute("DELETE FROM passages WHERE path = ?", (str(path),))
                                con.executemany("INSERT INTO passages VALUES (?, ?, ?, ?)",
                                                ((str(path), 0 if html else line, path.name, passage)
                                                 for line, passage in _passages(text)))
                                con.execute("INSERT OR REPLACE INTO files VALUES (?, ?)", (str(path), signature))
                            count += 1
                            report(f"Indexé · {path.name}")
                        except OSError as error:
                            errors.append(str(path))
                            report(f"{path.name} · {error}")
            if not errors:
                missing = [(path,) for path in indexed.keys() - seen]
                with con:
                    con.executemany("DELETE FROM passages WHERE path = ?", missing)
                    con.executemany("DELETE FROM files WHERE path = ?", missing)
            return count

    def search(self, query: str, *, count=6, cancel=None, roots: list[Path] | None = None) -> list[Source]:
        words = terms(query)
        if not words or not self.database.is_file():
            return []
        match = " OR ".join(f'"{word}"' for word in words)
        with self._open(read_only=True) as con:
            con.set_progress_handler(lambda: int(cancel is not None and cancel.is_set()), 3000)
            results = []
            seen = set()
            try:
                cursor = con.execute("""SELECT passages.path, line, title, text, signature
                    FROM passages JOIN files ON passages.path = files.path
                    WHERE passages MATCH ? ORDER BY bm25(passages, 0, 0, 1.5, 1) LIMIT ?""", (match, count * 5))
                for filename, line, title, passage, signature in cursor:
                    check_cancel(cancel)
                    path = Path(filename)
                    if path in seen or (roots is not None and not any(path.is_relative_to(root) for root in roots)):
                        continue
                    try:
                        if _signature(path.stat()) != signature or path.resolve() != path:
                            continue
                    except OSError:
                        continue
                    seen.add(path)
                    results.append(Source(f"{title} · ligne {line}" if line else title, passage, path.as_uri(), str(path.parent)))
                    if len(results) >= count:
                        break
            except sqlite3.OperationalError:
                check_cancel(cancel)
                raise
        return results


def research(query: str, documents: Documents, roots: list[Path], kiwix, book, cancel, report):
    found = []
    try:
        found = documents.search(query, count=3, cancel=cancel, roots=roots)
    except (OSError, sqlite3.Error) as error:
        report(f"Index personnel indisponible · {error}")
    api = kiwix.api
    if api is not None:
        candidates = [book] if book else kiwix.books[:2]
        keywords = terms(query)
        for candidate in candidates:
            try:
                hits = api.search(" ".join(keywords[:4]), candidate, count=3, cancel=cancel)
                for hit in hits:
                    excerpt = hit.excerpt or api.extract(hit, keywords, cancel)
                    if excerpt:
                        found.append(Source(hit.title, excerpt, hit.url, hit.collection))
            except (OSError, LocalServiceError) as error:
                report(f"{candidate.title} · {error}")
    return found[:5]
