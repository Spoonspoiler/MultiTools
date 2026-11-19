"""Doublons de contenu : audit, quarantaine locale sans écrasement, restauration.

Aucune suppression définitive. Aucun résultat IA n'autorise un déplacement.
"""
from collections import defaultdict
from dataclasses import dataclass
from contextlib import ExitStack
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from .inventory import Gate, BLOCK_SIZE
from .path_safety import (checked_stat, directory_handle, file_evidence, file_key,
                          open_regular, volume_at)

QUARANTINE = '.multitools-quarantaine'


@dataclass(frozen=True)
class Fingerprint:
    path: Path
    root: Path
    size: int
    sha256: str
    evidence: str


@dataclass(frozen=True)
class Duplicate:
    keep: Fingerprint
    extra: Fingerprint


def fingerprint(path, root, gate):
    before = file_evidence(path)
    digest = hashlib.sha256()
    with open_regular(path) as (stream, info):
        while block := stream.read(BLOCK_SIZE):
            gate.check()
            digest.update(block)
    if file_evidence(path) != before:
        raise OSError(f'Fichier modifié pendant l’analyse : {path}')
    return Fingerprint(path, root, info.st_size, digest.hexdigest(), before)


def audit(roots, gate, report=lambda _: None, exclude=()):
    """Lecture seule. Sources explicites ; aucune exploration des liens."""
    roots = sorted(set(Path(root).absolute() for root in roots), key=lambda p: (len(p.parts), str(p)))
    accepted = []
    for root in roots:
        checked_stat(root, directory=True)
        if not any(root.is_relative_to(other) for other in accepted):
            accepted.append(root)
    excluded = tuple(Path(p).absolute() for p in exclude)
    sizes = defaultdict(list)
    def fail(error):
        raise error
    for root in accepted:
        for folder, dirs, files in os.walk(root, followlinks=False, onerror=fail):
            gate.check()
            directory = Path(folder)
            if any(directory.is_relative_to(p) for p in excluded):
                dirs[:] = []
                continue
            dirs[:] = sorted(name for name in dirs if name != QUARANTINE
                             and not (directory / name).is_symlink()
                             and not (directory / name).is_junction())
            for name in sorted(files):
                gate.check()
                path = directory / name
                if path.is_symlink() or path.is_junction():
                    continue
                info = checked_stat(path)
                # Plusieurs noms sur un même inode : ne pas les déplacer automatiquement.
                if info.st_nlink > 1:
                    continue
                sizes[info.st_size].append((path, root))
    groups = defaultdict(list)
    for size, files in sizes.items():
        if len(files) < 2:
            continue
        for path, root in files:
            gate.check()
            report(str(path))
            item = fingerprint(path, root, gate)
            groups[(item.size, item.sha256)].append(item)
    plan = []
    for group in groups.values():
        if len(group) > 1:
            ordered = sorted(group, key=lambda item: str(item.path))
            plan.extend(Duplicate(ordered[0], item) for item in ordered[1:])
    return plan


def _rename(source, destination, source_fd, destination_fd):
    rename = getattr(ctypes.CDLL(None, use_errno=True), 'renameat2', None)
    if rename is None:
        raise OSError('renameat2 indisponible : opération refusée.')
    if rename(source_fd, os.fsencode(source), destination_fd, os.fsencode(destination), 1) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _verify(item, gate):
    if file_evidence(item.path) != item.evidence:
        raise OSError(f'Le fichier ou son volume a changé ; refaire l’analyse : {item.path}')
    current = fingerprint(item.path, item.root, gate)
    if current.sha256 != item.sha256 or current.size != item.size:
        raise OSError(f'Contenu modifié : {item.path}')


def quarantine(action, gate):
    """Une action explicitement confirmée ; jamais de déplacement inter-volume."""
    if not sys.platform.startswith('linux'):
        raise OSError('Quarantaine prise en charge uniquement sous Linux.')
    keep, extra = action.keep, action.extra
    if keep.path == extra.path or (keep.sha256, keep.size) != (extra.sha256, extra.size):
        raise ValueError('Deux fichiers distincts de contenu identique sont requis.')
    if not extra.path.is_relative_to(extra.root) or QUARANTINE in extra.path.relative_to(extra.root).parts:
        raise ValueError('Source hors périmètre.')
    root_info = checked_stat(extra.root, directory=True)
    if not volume_at(extra.root, root_info).local:
        raise OSError('Quarantaine refusée sur ce système de fichiers.')
    _verify(keep, gate)
    _verify(extra, gate)
    qroot = extra.root / QUARANTINE
    qroot.mkdir(mode=0o700, exist_ok=True)
    qinfo = checked_stat(qroot, directory=True)
    if qinfo.st_dev != checked_stat(extra.path).st_dev:
        raise OSError('La quarantaine doit être sur le même volume que le fichier.')
    entry = qroot / uuid4().hex
    entry.mkdir(mode=0o700)
    checked_stat(entry, directory=True)
    manifest = entry / 'manifest.json'
    record = {'version': 1, 'relative': str(extra.path.relative_to(extra.root)),
              'keeper': str(keep.path), 'sha256': extra.sha256, 'size': extra.size,
              'root_identity': [root_info.st_dev, root_info.st_ino]}
    with manifest.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    with ExitStack() as stack:
        srcfd = stack.enter_context(directory_handle(extra.path.parent))
        dstfd = stack.enter_context(directory_handle(entry))
        # Revalider après préparation du journal, avant publication atomique.
        gate.check()
        _verify(keep, gate)
        _verify(extra, gate)
        os.fsync(dstfd)
        with directory_handle(qroot) as fd:
            os.fsync(fd)
        with directory_handle(extra.root) as fd:
            os.fsync(fd)
        _rename(extra.path.name, 'payload', srcfd, dstfd)
        os.fsync(srcfd)
        os.fsync(dstfd)
    return manifest


def restore(manifest, gate):
    """Restauration sans écraser un fichier réapparu au chemin original."""
    if not sys.platform.startswith('linux'):
        raise OSError('Restauration disponible uniquement sous Linux.')
    manifest = Path(manifest).absolute()
    checked_stat(manifest)
    entry = manifest.parent
    checked_stat(entry, directory=True)
    qroot = entry.parent
    if qroot.name != QUARANTINE or manifest.name != 'manifest.json':
        raise ValueError('Journal de quarantaine invalide.')
    checked_stat(qroot, directory=True)
    root = qroot.parent
    record = json.loads(manifest.read_text())
    if record.get('version') != 1:
        raise ValueError('Version de journal inconnue.')
    relative = Path(record['relative'])
    if relative.is_absolute() or '..' in relative.parts or not relative.parts or QUARANTINE in relative.parts:
        raise ValueError('Chemin de restauration invalide.')
    info = checked_stat(root, directory=True)
    if [info.st_dev, info.st_ino] != record['root_identity']:
        raise OSError('Le volume source a changé ; restauration automatique refusée.')
    target = root / relative
    payload = entry / 'payload'
    result = fingerprint(payload, root, gate)
    if (result.sha256, result.size) != (record['sha256'], record['size']):
        raise OSError('La copie en quarantaine a changé.')
    with directory_handle(entry) as srcfd, directory_handle(target.parent) as dstfd:
        gate.check()
        if file_evidence(payload) != result.evidence:
            raise OSError('La quarantaine a changé pendant la restauration.')
        _rename('payload', target.name, srcfd, dstfd)
        os.fsync(srcfd)
        os.fsync(dstfd)
    return target
