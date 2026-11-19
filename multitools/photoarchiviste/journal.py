from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, fields
import json
import os
from pathlib import Path
import re
import sqlite3

from .inventory import Gate, Photo, inspect_photo, unique_roots, walk_photos
from .path_safety import checked_stat, directory_evidence, existing_parent, file_evidence, mount_snapshot


DEFAULT_CATEGORIES = (
    "À classer", "Famille et souvenirs", "Voyages", "Photogrammétrie", "Projets professionnels",
    "Captures WhatsApp", "Captures d’écran", "Documents",
)
PENDING = ("planned", "copying", "ready", "verified")
MOVE_DISABLED = "Mode conservatoire : le déplacement et le retrait des originaux sont désactivés."
LEGACY_COPY_BLOCKED = "Ancienne copie sans preuve de volume ou d’identité. Retire-la du plan et prépare une nouvelle copie après indexation."

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS photos (
    path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
    taken TEXT NOT NULL, format TEXT NOT NULL, camera TEXT NOT NULL,
    visual TEXT NOT NULL, color TEXT NOT NULL, warning TEXT NOT NULL,
    deferred INTEGER NOT NULL DEFAULT 0, seen INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
    sha256 TEXT NOT NULL, size INTEGER NOT NULL, decision TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL CHECK (mode IN ('copy', 'move')),
    kind TEXT NOT NULL CHECK (kind IN ('keep', 'reject', 'restore')),
    state TEXT NOT NULL DEFAULT 'planned', staging TEXT NOT NULL DEFAULT '',
    staged_dev TEXT, staged_ino TEXT,
    restore_of INTEGER REFERENCES transfers(id), error TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL DEFAULT (datetime('now')), finished TEXT
);
CREATE INDEX IF NOT EXISTS transfers_target ON transfers(target);
CREATE INDEX IF NOT EXISTS transfers_source ON transfers(source, sha256);
CREATE INDEX IF NOT EXISTS transfers_restore ON transfers(restore_of);
CREATE TABLE IF NOT EXISTS photo_evidence (
    path TEXT PRIMARY KEY REFERENCES photos(path) ON DELETE CASCADE, evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transfer_evidence (
    id INTEGER PRIMARY KEY REFERENCES transfers(id), source TEXT NOT NULL,
    anchor TEXT NOT NULL, destination TEXT NOT NULL, parent TEXT
);
"""

TRANSFER_QUERY = """
SELECT t.*, EXISTS (SELECT 1 FROM transfer_evidence e WHERE e.id = t.id) AS guarded
FROM transfers t
"""


@dataclass(frozen=True)
class Transfer:
    id: int
    source: Path
    target: Path
    sha256: str
    size: int
    decision: str
    category: str
    detail: str
    mode: str
    kind: str
    state: str
    staging: str
    staged_dev: str | None
    staged_ino: str | None
    restore_of: int | None
    error: str
    created: str
    finished: str | None
    guarded: bool = False

    @property
    def blocked(self) -> str:
        if self.mode != "copy":
            return MOVE_DISABLED
        return "" if self.guarded else LEGACY_COPY_BLOCKED

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Transfer:
        values = dict(row)
        values.update(source=Path(row["source"]), target=Path(row["target"]), guarded=bool(row["guarded"]))
        return cls(**values)


@dataclass(frozen=True)
class Choice:
    photo: Photo
    category: str | None


@contextmanager
def _workspace_lock(path: Path):
    with path.open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise OSError("Ce journal est déjà utilisé par une autre instance de PhotoArchiviste.") from error
        yield


def category_parts(category: str) -> tuple[str, ...]:
    parts = tuple(part.strip() for part in category.split("/"))
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith(".")
                or re.search(r'[\\:*?"<>|\x00-\x1f]', part)
                or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)):
            raise ValueError("Catégorie invalide. Pour une sous-catégorie, utilise par exemple Projets/Photogrammétrie.")
    return parts


class Archive:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def __enter__(self) -> Archive:
        with ExitStack() as stack:
            self.root_evidence, self.volume = directory_evidence(self.root)
            if not self.volume.local:
                raise OSError("Le journal PhotoArchiviste doit rester sur un système de fichiers local pris en charge.")
            journal = self.root / "PhotoArchiviste.sqlite3"
            lock = self.root / ".photoarchiviste.lock"
            for path in (journal, lock, *(journal.with_name(journal.name + suffix)
                                          for suffix in ("-journal", "-wal", "-shm"))):
                try:
                    info = checked_stat(path)
                except FileNotFoundError:
                    continue
                if info.st_nlink != 1 or info.st_dev != self.root.stat().st_dev:
                    raise OSError(f"Fichier de journal lié ou situé sur un autre volume : {path}")
            stack.enter_context(_workspace_lock(lock))
            self.check_workspace()
            self.db = sqlite3.connect(journal)
            stack.callback(self.db.close)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys = ON")
            self.db.execute("PRAGMA synchronous = FULL")
            self.db.executescript(SCHEMA)
            self.resources = stack.pop_all()
        return self

    def __exit__(self, *_exc) -> None:
        self.resources.close()

    def _setting(self, key: str, fallback: str) -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else fallback

    @property
    def roots(self) -> list[Path]:
        return [Path(root) for root in json.loads(self._setting("roots", "[]"))]

    @property
    def scan_incomplete(self) -> bool:
        return self._setting("scan_incomplete", "0") == "1"

    def photos(self, *, include_unseen: bool = False) -> list[Photo]:
        columns = ", ".join(field.name for field in fields(Photo))
        where = "" if include_unseen else " WHERE seen = 1"
        return [Photo(**{**dict(row), "path": Path(row["path"]), "deferred": bool(row["deferred"])})
                for row in self.db.execute(f"SELECT {columns} FROM photos{where} ORDER BY path")]

    def scan(self, roots: list[Path], gate: Gate, report, *, on_photo=None) -> tuple[int, int]:
        self.check_workspace()
        roots = unique_roots(roots, self.root)
        if not roots:
            raise ValueError("Sélectionne au moins un dossier source.")
        resuming = self.scan_incomplete and roots == self.roots
        cached = {photo.path: photo for photo in self.photos(include_unseen=True)} if resuming else {}
        evidence = dict(self.db.execute("SELECT path, evidence FROM photo_evidence")) if resuming else {}
        mounts = mount_snapshot()
        names = [str(root) for root in roots]
        with self.db:
            self.db.execute("UPDATE photos SET seen = 0")
            self.db.executemany("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                                [("roots", json.dumps(names)), ("scan_incomplete", "1")])
        visited = errors = 0
        incomplete = False

        def on_error(error):
            nonlocal errors
            errors += 1
            report("error", str(error))

        def on_read_error(error):
            nonlocal incomplete
            incomplete = True
            on_error(error)

        for root in roots:
            gate.check()
            for path in walk_photos(root, self.root, on_read_error):
                gate.check()
                report("file", f"Indexation · {path}")
                try:
                    before = file_evidence(path, mounts)
                    photo = cached.get(path)
                    if photo is None or before != evidence.get(str(path)):
                        photo = inspect_photo(path, gate)
                    if file_evidence(path, mounts) != before:
                        raise OSError(f"Le fichier a changé pendant l’indexation : {path}")
                    self._save_photo(photo, before)
                    visited += 1
                    if on_photo is not None:
                        on_photo(photo)
                    if photo.warning:
                        on_error(f"Aperçu indisponible pour {path.name} · {photo.warning}")
                except OSError as error:
                    on_read_error(error)
        self.check_workspace()
        if not incomplete:
            with self.db:
                self.db.execute("DELETE FROM photos WHERE seen = 0")
                self.db.execute("UPDATE settings SET value = '0' WHERE key = 'scan_incomplete'")
        return visited, errors

    def _save_photo(self, photo: Photo, evidence: str) -> None:
        values = asdict(photo)
        values["path"] = str(photo.path)
        names = tuple(values)
        update = [f"{name} = excluded.{name}" for name in names if name not in {"path", "deferred"}]
        update.extend(("seen = 1", "deferred = CASE WHEN photos.sha256 = excluded.sha256 THEN photos.deferred ELSE 0 END"))
        with self.db:
            self.db.execute(f"INSERT INTO photos ({', '.join(names)}) VALUES ({', '.join(':'+name for name in names)}) "
                            f"ON CONFLICT(path) DO UPDATE SET {', '.join(update)}", values)
            self.db.execute("INSERT OR REPLACE INTO photo_evidence VALUES (?, ?)", (str(photo.path), evidence))

    def defer(self, photos: tuple[Photo, ...], deferred: bool = True) -> None:
        with self.db:
            self.db.executemany("UPDATE photos SET deferred = ? WHERE path = ? AND sha256 = ?",
                                [(deferred, str(photo.path), photo.sha256) for photo in photos])

    def withheld(self) -> set[Path]:
        rows = self.db.execute("""
            SELECT p.path FROM photos p JOIN transfers t ON t.source = p.path AND t.sha256 = p.sha256
            WHERE t.restore_of IS NULL AND t.state <> 'cancelled' AND NOT EXISTS (
                SELECT 1 FROM transfers r WHERE r.restore_of = t.id AND r.state = 'done'
            )
        """)
        return {Path(row[0]) for row in rows}

    def transfers(self) -> list[Transfer]:
        return [Transfer.from_row(row) for row in self.db.execute(TRANSFER_QUERY + " ORDER BY t.id")]

    def transfer(self, transfer_id: int) -> Transfer:
        row = self.db.execute(TRANSFER_QUERY + " WHERE t.id = ?", (transfer_id,)).fetchone()
        if row is None:
            raise ValueError("Transfert introuvable.")
        return Transfer.from_row(row)

    def available(self, desired: Path) -> Path:
        number = 1
        while True:
            candidate = desired if number == 1 else desired.with_name(f"{desired.stem} ({number}){desired.suffix}")
            pending = self.db.execute("""SELECT 1 FROM transfers WHERE target = ?
                                      AND state NOT IN ('done', 'cancelled')""", (str(candidate),)).fetchone()
            if not pending and not os.path.lexists(candidate):
                return candidate
            number += 1

    def check_destination(self, target: Path, kind: str) -> None:
        if not target.is_absolute() or ".." in target.parts:
            raise OSError(f"Destination invalide : {target}")
        if kind == "restore":
            checked_stat(target.parent, directory=True)
            return
        base = self.root / ("A_JETER" if kind == "reject" else "TRIEES")
        if not target.is_relative_to(base):
            raise OSError("La destination doit rester dans le dossier d’archive, sans lien symbolique.")
        existing_parent(target)

    def check_workspace(self) -> None:
        if directory_evidence(self.root)[0] != self.root_evidence:
            raise OSError("Le dossier de travail ou son montage a changé.")

    def _record_evidence(self, transfer_id: int, source: str, target: Path, kind: str) -> None:
        anchor = existing_parent(target)
        destination, volume = directory_evidence(anchor)
        if not volume.local or (kind != "restore" and volume != self.volume):
            raise OSError(f"La destination doit être sur un volume local autorisé : {target}")
        self.db.execute("INSERT INTO transfer_evidence VALUES (?, ?, ?, ?, NULL)",
                        (transfer_id, source, str(anchor), destination))

    def check_transfer_paths(self, item: Transfer, *, pin_parent=False) -> None:
        if item.blocked:
            raise OSError(item.blocked)
        self.check_workspace()
        self.check_destination(item.target, item.kind)
        proof = self.db.execute("SELECT * FROM transfer_evidence WHERE id = ?", (item.id,)).fetchone()
        source = file_evidence(item.source)
        if source != proof["source"]:
            raise OSError(f"La source a changé depuis l’indexation ou la préparation : {item.source}")
        anchor = Path(proof["anchor"])
        if not item.target.parent.is_relative_to(anchor):
            raise OSError(f"La destination a quitté son emplacement autorisé : {item.target}")
        destination, volume = directory_evidence(anchor)
        if destination != proof["destination"]:
            raise OSError(f"Le dossier de destination ou son montage a changé : {anchor}")
        parent = existing_parent(item.target)
        current, current_volume = directory_evidence(parent)
        if current_volume != volume or (proof["parent"] is not None and current != proof["parent"]):
            raise OSError(f"Le dossier parent ou son montage a changé : {item.target.parent}")
        if pin_parent:
            if parent != item.target.parent:
                raise OSError(f"La destination est indisponible : {item.target.parent}")
            if proof["parent"] is None:
                with self.db:
                    self.db.execute("UPDATE transfer_evidence SET parent = ? WHERE id = ?", (current, item.id))
    def plan(self, choices: list[Choice], decision: str, detail: str, *, move: bool, by_year: bool,
             gate: Gate | None = None) -> list[Transfer]:
        if move:
            raise ValueError(MOVE_DISABLED)
        self.check_workspace()
        withheld = self.withheld()
        mounts = mount_snapshot()
        ids = []
        with self.db:
            for choice in choices:
                if gate is not None:
                    gate.check()
                photo = choice.photo
                if photo.path in withheld:
                    raise ValueError(f"Une décision existe déjà pour {photo.path.name}.")
                withheld.add(photo.path)
                proof = self.db.execute("""
                    SELECT e.evidence FROM photo_evidence e JOIN photos p USING (path)
                    WHERE p.path = ? AND p.sha256 = ? AND p.seen = 1
                """, (str(photo.path), photo.sha256)).fetchone()
                if proof is None or file_evidence(photo.path, mounts) != proof[0]:
                    raise OSError(f"Source non vérifiable. Relance l’indexation : {photo.path}")
                if choice.category is None:
                    kind, category = "reject", ""
                    folder = self.root / "A_JETER"
                else:
                    kind, category = "keep", "/".join(category_parts(choice.category))
                    folder = self.root / "TRIEES" / category
                    if by_year and photo.taken:
                        folder /= photo.taken[:4]
                target = self.available(folder / photo.path.name)
                self.check_destination(target, kind)
                cursor = self.db.execute("""
                    INSERT INTO transfers (source, target, sha256, size, decision, category, detail, mode, kind)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (str(photo.path), str(target), photo.sha256, photo.size, decision, category, detail,
                      "copy", kind))
                self._record_evidence(cursor.lastrowid, proof[0], target, kind)
                ids.append(cursor.lastrowid)
        return [self.transfer(transfer_id) for transfer_id in ids]

    def plan_restore(self, transfer_ids: list[int], *, copy: bool = False) -> int:
        if not copy:
            raise ValueError("La restauration par déplacement est désactivée. Choisis explicitement la restauration par copie.")
        self.check_workspace()
        count = 0
        with self.db:
            for transfer_id in transfer_ids:
                original = self.transfer(transfer_id)
                reverse = self.db.execute("SELECT 1 FROM transfers WHERE restore_of = ? AND state <> 'cancelled'",
                                          (transfer_id,)).fetchone()
                if original.state != "done" or original.kind == "restore" or reverse or original.blocked:
                    continue
                proof = self.db.execute("SELECT source, parent FROM transfer_evidence WHERE id = ?", (original.id,)).fetchone()
                parent, volume = directory_evidence(original.source.parent)
                source_proof = json.loads(proof["source"])
                if (not volume.local or (volume.mount, volume.identity) != tuple(source_proof[1])
                        or json.loads(parent)[:2] != source_proof[2]):
                    raise OSError(f"Le volume original n’est pas disponible pour la restauration : {original.source.parent}")
                if directory_evidence(original.target.parent)[0] != proof["parent"]:
                    raise OSError(f"Le dossier de la copie a changé : {original.target.parent}")
                copy_info = checked_stat(original.target)
                if (str(copy_info.st_dev), str(copy_info.st_ino)) != (original.staged_dev, original.staged_ino):
                    raise OSError(f"La copie d’origine a été remplacée : {original.target}")
                target = self.available(original.source)
                self.check_destination(target, "restore")
                cursor = self.db.execute("""
                    INSERT INTO transfers (source, target, sha256, size, decision, detail, mode, kind, restore_of)
                    VALUES (?, ?, ?, ?, ?, ?, 'copy', 'restore', ?)
                """, (str(original.target), str(target), original.sha256, original.size, "Restaurer par copie",
                      original.decision, transfer_id))
                self._record_evidence(cursor.lastrowid, file_evidence(original.target), target, "restore")
                if directory_evidence(target.parent)[0] != parent:
                    raise OSError(f"Le dossier original a changé : {target.parent}")
                count += 1
        return count

    def cancel_plans(self, transfer_ids: list[int]) -> int:
        with self.db:
            changed = self.db.executemany("""
                UPDATE transfers SET state = 'cancelled'
                WHERE id = ? AND state IN ('planned', 'copying', 'ready', 'verified')
            """,
                                          [(transfer_id,) for transfer_id in transfer_ids])
        return changed.rowcount
