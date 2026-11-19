"""Inventaire local et API publique de kiwix-serve."""

from collections import deque
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
import os
from pathlib import Path
import shutil
import socket
import subprocess
from threading import Thread
from time import monotonic, sleep
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree as ET

from .local_http import HttpStatusError, LocalHTTP, LocalServiceError, check_cancel


@dataclass(frozen=True)
class ArchiveFile:
    path: Path
    size: int
    date: str


@dataclass(frozen=True)
class Book:
    name: str
    title: str
    language: str
    date: str
    url: str


@dataclass(frozen=True)
class Source:
    title: str
    excerpt: str
    url: str
    collection: str = ""


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, _attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "li", "div", "h1", "h2", "h3", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_html(data: str) -> str:
    parser = PlainHTML()
    parser.feed(data)
    parser.close()
    return "".join(parser.parts)


def inventory(roots: list[Path], files: list[Path], cancel=None, report=lambda _: None) -> list[ArchiveFile]:
    paths = set()
    for root in roots:
        if not root.is_dir():
            report(f"Dossier indisponible · {root}")
            continue
        for folder, dirs, entries in os.walk(root, onerror=lambda error: report(str(error))):
            check_cancel(cancel)
            dirs[:] = [name for name in dirs if not (Path(folder) / name).is_junction()]
            paths.update(Path(folder) / name for name in entries if Path(name).suffix.lower() in {".zim", ".zimaa"})
    result = {}
    for path in sorted(paths | set(files)):
        check_cancel(cancel)
        try:
            path = path.resolve(strict=True)
            if not path.is_file() or path.suffix.lower() not in {".zim", ".zimaa"}:
                continue
            stat = path.stat()
            size = stat.st_size
            if path.suffix.lower() == ".zimaa":
                pattern = path.name[:-2] + "[a-z][a-z]"
                size = sum(part.stat().st_size for part in path.parent.glob(pattern))
            result[path] = ArchiveFile(path, size, date.fromtimestamp(stat.st_mtime).isoformat())
        except OSError as error:
            report(str(error))
    return list(result.values())


def _xml(data: bytes) -> ET.Element:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise LocalServiceError("La version Kiwix installée a renvoyé un format XML incompatible.") from error
    for item in root.iter():
        item.tag = item.tag.split("}")[-1]
    return root


class KiwixAPI:
    def __init__(self, host: str, port: int):
        self.http = LocalHTTP(host, port, lan=True)

    @property
    def url(self) -> str:
        return self.http.base + "/"

    def _page(self, value: str) -> str:
        if not value or value.startswith("//") or "\\" in value:
            return ""
        absolute = urljoin(self.url, value)
        target, origin = urlsplit(absolute), urlsplit(self.url)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc) or not target.path.startswith("/content/"):
            return ""
        return absolute

    def books(self, cancel=None) -> list[Book]:
        result = []
        seen = set()
        for start in range(0, 10000, 50):
            feed = _xml(self.http.read(f"/catalog/v2/entries?start={start}&count=50", cancel=cancel))
            if feed.tag != "feed":
                raise LocalServiceError("Le serveur ne fournit pas de catalogue OPDS Kiwix v2.")
            entries = feed.findall("entry")
            added = 0
            for entry in entries:
                link = next((link.get("href", "") for link in entry.findall("link") if link.get("type") == "text/html"), "")
                url = self._page(link)
                if not url:
                    continue
                name = unquote(urlsplit(url).path.split("/")[2])
                if not name or name in seen:
                    continue
                seen.add(name)
                added += 1
                result.append(Book(name, entry.findtext("title") or name, entry.findtext("language", ""),
                                   entry.findtext("issued", ""), url))
            if len(entries) < 50 or not added:
                break
        return result

    def search(self, query: str, book: Book, *, count=12, cancel=None) -> list[Source]:
        if not query.strip():
            return []
        params = urlencode({"pattern": query[:500], "books.name": book.name, "content": book.name,
                            "pageLength": count, "format": "xml"})
        try:
            response = self.http.read(f"/search?{params}", cancel=cancel, timeout=20)
        except HttpStatusError as error:
            if error.status == 404:
                raise LocalServiceError(f"Recherche plein texte indisponible pour {book.title}.") from error
            raise
        feed = _xml(response)
        if feed.tag != "rss":
            raise LocalServiceError("La recherche Kiwix ne renvoie pas le format RSS attendu.")
        results = []
        for item in feed.findall("./channel/item")[:count]:
            url = self._page(item.findtext("link", ""))
            if url:
                excerpt = next(("".join(node.itertext()) for node in item.findall("description")), "")
                excerpt = " ".join(plain_html(excerpt).split())
                results.append(Source(item.findtext("title", "Article"), excerpt, url, book.title))
        return results

    def extract(self, source: Source, terms: list[str], cancel=None) -> str:
        url = self._page(source.url)
        if not url:
            return ""
        name, separator, article = urlsplit(url).path.removeprefix("/content/").partition("/")
        if not separator or not article:
            return ""
        route = f"/raw/{quote(unquote(name), safe='')}/content/{quote(unquote(article), safe='/')}"
        body = self.http.read(route, cancel=cancel, limit=2 * 1024 * 1024)
        paragraphs = [line.strip() for line in plain_html(body.decode("utf-8", "replace")).splitlines() if len(line.strip()) >= 45]
        ranked = [(sum(term.casefold() in line.casefold() for term in terms), line) for line in paragraphs]
        score, best = max(ranked, default=(0, ""), key=lambda pair: pair[0])
        return best[:850] if score else ""


class KiwixServer:
    def __init__(self):
        self.process = None
        self.api = None
        self.books = []
        self.messages = deque(maxlen=12)
        self.version = ""

    @staticmethod
    def executable():
        return shutil.which("kiwix-serve")

    def connect(self, host, port, cancel=None) -> list[Book]:
        api = KiwixAPI(host, port)
        if self.process is not None and self.process.poll() is None and self.api is not None and api.url != self.api.url:
            raise LocalServiceError("Arrête le serveur de Multitools avant de connecter une autre adresse.")
        books = api.books(cancel)
        self.api, self.books = api, books
        return books

    def start(self, host, port, files: list[ArchiveFile], cancel=None) -> list[Book]:
        if self.process is not None and self.process.poll() is None:
            raise LocalServiceError("Arrête le serveur de Multitools avant de changer son catalogue.")
        if not files:
            raise ValueError("Sélectionne d’abord des archives ZIM.")
        executable = self.executable()
        if not executable:
            raise LocalServiceError("kiwix-serve est absent du PATH. Les archives restent consultables dans l’inventaire.")
        api = KiwixAPI(host, port)
        try:
            with socket.create_connection((api.http.host, api.http.port), timeout=1):
                raise LocalServiceError("Le port est occupé. Utilise Connecter ou choisis un autre port.")
        except OSError:
            pass
        options = subprocess.run([executable, "--help"], capture_output=True, text=True, errors="replace", timeout=5)
        help_text = options.stdout + options.stderr
        if "--address" not in help_text or "--blockexternal" not in help_text:
            raise LocalServiceError("Cette version de kiwix-serve ne propose pas les options réseau requises.")
        version = subprocess.run([executable, "--version"], capture_output=True, text=True, errors="replace", timeout=5)
        self.version = ((version.stdout or version.stderr).strip().splitlines() or [""])[0]
        command = [executable, f"--address={api.http.host}", f"--port={api.http.port}", "--blockexternal"]
        for option, value in (("--threads", 2), ("--searchLimit", 4), ("--attachToProcess", os.getpid())):
            if option in help_text:
                command.append(f"{option}={value}")
        command.extend(str(item.path) for item in files)
        check_cancel(cancel)
        self.messages.clear()
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 text=True, errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.process = child

        def capture():
            with child.stderr:
                for line in child.stderr:
                    self.messages.append(line.strip()[:500])
        Thread(target=capture, daemon=True).start()
        try:
            deadline = monotonic() + 15
            while monotonic() < deadline:
                if child.poll() is not None:
                    raise LocalServiceError("kiwix-serve s’est arrêté. " + " · ".join(self.messages))
                check_cancel(cancel)
                try:
                    books = api.books(cancel)
                except HttpStatusError:
                    raise
                except LocalServiceError:
                    sleep(0.25)
                else:
                    if child.poll() is None:
                        self.api, self.books = api, books
                        return books
            raise LocalServiceError("Catalogue OPDS v2 non disponible après démarrage de Kiwix.")
        except BaseException:
            self.stop()
            raise

    def stop(self):
        child = self.process
        self.api, self.books = None, []
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)
        self.process = None
