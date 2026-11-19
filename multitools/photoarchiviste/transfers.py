from __future__ import annotations

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import re
import shutil
from uuid import uuid4

from .inventory import BLOCK_SIZE, Gate
from .journal import Archive, PENDING, Transfer
from .path_safety import checked_stat, directory_handle, existing_parent, file_key, make_destination_parent, open_regular


def _check_digest(path: Path, item: Transfer, gate: Gate, *, dir_fd=None):
    with open_regular(path, dir_fd=dir_fd) as (stream, before):
        if before.st_size != item.size:
            raise OSError(f"La taille diffère du fichier journalisé : {path}")
        opened = file_key(os.fstat(stream.fileno()))
        digest = hashlib.sha256()
        remaining = item.size
        while remaining:
            gate.check()
            block = stream.read(min(BLOCK_SIZE, remaining))
            if not block:
                break
            digest.update(block)
            remaining -= len(block)
        if remaining or stream.read(1) or digest.hexdigest() != item.sha256:
            raise OSError(f"Le contenu diffère du fichier journalisé : {path}")
        expected = file_key(before)
        if file_key(os.fstat(stream.fileno())) != opened or file_key(checked_stat(path)) != expected:
            raise OSError(f"Le fichier a changé pendant sa vérification : {path}")
        return expected


def _staging_path(item: Transfer) -> Path:
    if not re.fullmatch(r"\.photoarchiviste-[0-9a-f]{32}\.part", item.staging):
        raise OSError("Nom de fichier de transfert temporaire invalide.")
    return item.target.parent / item.staging


def _sync_destination(archive: Archive, item: Transfer) -> None:
    if os.name == "nt":
        return
    directory = item.target.parent
    while True:
        with directory_handle(directory) as fd:
            os.fsync(fd)
        if item.kind == "restore" or directory == archive.root:
            return
        directory = directory.parent


def _publish_no_replace(staging: Path, target: Path, *, dir_fd=None) -> None:
    if os.name == "nt":
        staging.rename(target)
        return
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if rename is None or dir_fd is None:
        raise OSError("La publication atomique sans écrasement n’est pas disponible.")
    if rename(dir_fd, os.fsencode(staging.name), dir_fd, os.fsencode(target.name), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def _set_copying(archive: Archive, item: Transfer) -> Transfer:
    with archive.db:
        archive.db.execute("UPDATE transfers SET state = 'copying', staging = ?, staged_dev = NULL, staged_ino = NULL WHERE id = ?",
                           (f".photoarchiviste-{uuid4().hex}.part", item.id))
    return archive.transfer(item.id)


def _preserve_timestamps(fd: int, source: os.stat_result) -> None:
    if os.name != "nt":
        os.utime(fd, ns=(source.st_atime_ns, source.st_mtime_ns))
        return
    from ctypes import wintypes
    from msvcrt import get_osfhandle

    def filetime(nanoseconds):
        ticks = nanoseconds // 100 + 116_444_736_000_000_000
        return wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)

    accessed = filetime(source.st_atime_ns)
    modified = filetime(source.st_mtime_ns)
    set_time = ctypes.WinDLL("kernel32", use_last_error=True).SetFileTime
    set_time.argtypes = (wintypes.HANDLE, wintypes.LPFILETIME, wintypes.LPFILETIME, wintypes.LPFILETIME)
    if not set_time(get_osfhandle(fd), None, ctypes.byref(accessed), ctypes.byref(modified)):
        raise ctypes.WinError(ctypes.get_last_error())


def _stage(archive: Archive, item: Transfer, gate: Gate) -> Transfer:
    archive.check_transfer_paths(item)
    if item.kind != "restore":
        make_destination_parent(item.target)
    archive.check_transfer_paths(item, pin_parent=True)
    temporary = _staging_path(item)
    with directory_handle(item.target.parent) as dir_fd, open_regular(item.source) as (source, before):
        archive.check_transfer_paths(item)
        opened = file_key(os.fstat(source.fileno()))
        # A fresh exclusive name is used for each attempt. An old name is never unlinked or reused.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(temporary if dir_fd is None else temporary.name, flags, 0o600, dir_fd=dir_fd)
        with os.fdopen(fd, "wb") as output:
            remaining = item.size
            while remaining:
                gate.check()
                block = source.read(min(BLOCK_SIZE, remaining))
                if not block or output.write(block) != len(block):
                    raise OSError(f"Copie incomplète : {item.source}")
                remaining -= len(block)
            if source.read(1) or file_key(os.fstat(source.fileno())) != opened:
                raise OSError(f"La source a changé pendant la copie : {item.source}")
            output.flush()
            _preserve_timestamps(output.fileno(), before)
            os.fsync(output.fileno())
            written = os.fstat(output.fileno())
        archive.check_transfer_paths(item)
        staged = _check_digest(temporary, item, gate, dir_fd=dir_fd)
        if staged[:2] != (written.st_dev, written.st_ino):
            raise OSError(f"Le temporaire a été remplacé : {temporary}")
        if dir_fd is not None:
            os.fsync(dir_fd)
    archive.check_transfer_paths(item)
    with archive.db:
        archive.db.execute("UPDATE transfers SET state = 'ready', staged_dev = ?, staged_ino = ? WHERE id = ?",
                           (str(staged[0]), str(staged[1]), item.id))
    return archive.transfer(item.id)


def _check_staged_identity(path: Path, item: Transfer, verified) -> None:
    if (str(verified[0]), str(verified[1])) != (item.staged_dev, item.staged_ino):
        raise OSError(f"La provenance du fichier ne peut pas être confirmée : {path}")


def _publish(archive: Archive, item: Transfer, gate: Gate) -> Transfer:
    archive.check_transfer_paths(item)
    temporary = _staging_path(item)
    with directory_handle(item.target.parent) as dir_fd:
        try:
            temporary.lstat()
        except FileNotFoundError:
            # A rename may have completed immediately before an interruption.
            verified = _check_digest(item.target, item, gate, dir_fd=dir_fd)
            _check_staged_identity(item.target, item, verified)
        else:
            staged = _check_digest(temporary, item, gate, dir_fd=dir_fd)
            _check_staged_identity(temporary, item, staged)
            while True:
                gate.check()
                archive.check_transfer_paths(item)
                if file_key(checked_stat(temporary)) != staged:
                    raise OSError(f"Le temporaire a changé avant publication : {temporary}")
                try:
                    _publish_no_replace(temporary, item.target, dir_fd=dir_fd)
                except FileExistsError:
                    target = archive.available(item.target)
                    with archive.db:
                        archive.db.execute("UPDATE transfers SET target = ? WHERE id = ?", (str(target), item.id))
                    item = archive.transfer(item.id)
                else:
                    break
            verified = _check_digest(item.target, item, gate, dir_fd=dir_fd)
            _check_staged_identity(item.target, item, verified)
    _sync_destination(archive, item)
    archive.check_transfer_paths(item)
    if file_key(checked_stat(item.target)) != verified:
        raise OSError(f"La destination a changé après publication : {item.target}")
    with archive.db:
        archive.db.execute("UPDATE transfers SET state = 'verified' WHERE id = ?", (item.id,))
    return archive.transfer(item.id)


def _finish(archive: Archive, item: Transfer, gate: Gate) -> None:
    archive.check_transfer_paths(item)
    with directory_handle(item.target.parent) as dir_fd:
        verified = _check_digest(item.target, item, gate, dir_fd=dir_fd)
        _check_staged_identity(item.target, item, verified)
        gate.check()
        with archive.db:
            archive.db.execute("UPDATE transfers SET state = 'done', error = '', finished = datetime('now') WHERE id = ?",
                               (item.id,))
            # Check again inside the transaction so a failed observation rolls back the success.
            archive.check_transfer_paths(item)
            if file_key(checked_stat(item.target)) != verified:
                raise OSError(f"La destination a changé après vérification : {item.target}")


def execute_pending(archive: Archive, gate: Gate, report, preflight=None) -> tuple[int, int]:
    entries = [item for item in archive.transfers() if item.state in PENDING]
    done = errors = 0
    for step, item in enumerate(entries, 1):
        gate.check()
        report("file", f"{item.source} → {item.target}")
        try:
            archive.check_transfer_paths(item)
            copying = item.state in {"planned", "copying"}
            if preflight is not None:
                preflight(item.target, item.size if copying else 0)
            if copying:
                ancestor = existing_parent(item.target)
                if shutil.disk_usage(ancestor).free < item.size:
                    raise OSError(errno.ENOSPC, "Espace disponible insuffisant", str(ancestor))
                item = _stage(archive, _set_copying(archive, item), gate)
            if item.state == "ready":
                item = _publish(archive, item, gate)
            _finish(archive, item, gate)
        except OSError as error:
            with archive.db:
                archive.db.execute("UPDATE transfers SET error = ? WHERE id = ?", (str(error), item.id))
            report("error", f"{item.source.name} · {error}")
            errors += 1
        else:
            report("done", f"{item.target.name} · copié et vérifié")
            done += 1
        report("progress", (step, len(entries)))
    return done, errors
