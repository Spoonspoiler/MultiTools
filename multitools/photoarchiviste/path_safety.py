"""Identify the files, directories and mounts used by a copy."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import ctypes
import json
import os
from pathlib import Path
import re
import stat
import sys


LOCAL_LINUX_FILESYSTEMS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "ntfs3"}


@dataclass(frozen=True)
class Volume:
    mount: str
    identity: str
    local: bool


def mount_snapshot():
    if not sys.platform.startswith("linux"):
        return None
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    mounts = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        mount_id, _, device, _, point, *_ = left.split()
        filesystem, source, *_ = right.split()
        point = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), point)
        mounts.append((Path(point), device,
                       Volume(point, f"{boot}:{mount_id}:{device}:{filesystem}:{source}",
                              filesystem in LOCAL_LINUX_FILESYSTEMS)))
    return mounts


def windows_volume(path: Path) -> Volume:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    mount = ctypes.create_unicode_buffer(32768)
    if not kernel.GetVolumePathNameW(str(path), mount, len(mount)):
        raise ctypes.WinError(ctypes.get_last_error())
    drive_type = kernel.GetDriveTypeW(mount)
    if drive_type == 4:  # Network path. Resolve a mapped drive to its UNC share.
        share = Path(mount.value).drive
        if not share.startswith("\\\\"):
            name = ctypes.create_unicode_buffer(32768)
            length = ctypes.c_ulong(len(name))
            error = ctypes.WinDLL("mpr").WNetGetConnectionW(share, name, ctypes.byref(length))
            if error:
                raise ctypes.WinError(error)
            share = name.value
        return Volume(mount.value, share.casefold(), False)
    guid = ctypes.create_unicode_buffer(1024)
    if drive_type not in {2, 3} or not kernel.GetVolumeNameForVolumeMountPointW(mount, guid, len(guid)):
        raise OSError(f"Impossible d’identifier le volume local : {path}")
    return Volume(mount.value, guid.value.casefold(), True)


def volume_at(path: Path, info: os.stat_result, mounts=None) -> Volume:
    if os.name == "nt":
        return windows_volume(path)
    if sys.platform.startswith("linux"):
        mounts = mount_snapshot() if mounts is None else mounts
        candidates = [(point, device, volume) for point, device, volume in mounts if path.is_relative_to(point)]
        if candidates:
            deepest = max(len(point.parts) for point, _, _ in candidates)
            device_id = f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}"
            matching = [volume for point, device, volume in candidates
                        if len(point.parts) == deepest and device == device_id]
            if len(matching) == 1:
                return matching[0]
    raise OSError(f"Impossible d’identifier le montage : {path}")


def checked_stat(path: Path, *, directory=False) -> os.stat_result:
    info = path.lstat()
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if not expected_type(info.st_mode) or reparse or not info.st_ino:
        raise OSError(f"Chemin non vérifiable ou situé derrière un lien : {path}")
    try:
        resolved = path.resolve(strict=True)
    except RuntimeError as error:
        raise OSError(f"Impossible de résoudre le chemin : {path}") from error
    if resolved != path:
        raise OSError(f"Chemin non vérifiable ou situé derrière un lien : {path}")
    return info


def file_key(info: os.stat_result):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def file_evidence(path: Path, mounts=None) -> str:
    info = checked_stat(path)
    volume = volume_at(path, info, mounts)
    parent = checked_stat(path.parent, directory=True)
    return json.dumps((file_key(info), (volume.mount, volume.identity), (parent.st_dev, parent.st_ino)))


def directory_evidence(path: Path) -> tuple[str, Volume]:
    info = checked_stat(path, directory=True)
    volume = volume_at(path, info)
    return json.dumps((info.st_dev, info.st_ino, volume.mount, volume.identity)), volume


def existing_parent(path: Path) -> Path:
    for parent in path.parents:
        try:
            checked_stat(parent, directory=True)
        except FileNotFoundError:
            continue
        return parent
    raise OSError(f"Aucun dossier parent disponible : {path}")


def make_destination_parent(target: Path) -> None:
    ancestor = existing_parent(target)
    components = target.parent.relative_to(ancestor).parts
    if components:
        with ExitStack() as handles:
            fd = handles.enter_context(directory_handle(ancestor))
            _, volume = directory_evidence(ancestor)
            directory = ancestor
            for name in components:
                directory /= name
                try:
                    os.mkdir(directory if fd is None else name, dir_fd=fd)
                except FileExistsError:
                    pass
                named = checked_stat(directory, directory=True)
                if volume_at(directory, named) != volume:
                    raise OSError(f"Le dossier a changé de montage : {directory}")
                if fd is not None:
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    handles.callback(os.close, fd)
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                        raise OSError(f"Le dossier a changé : {directory}")


@contextmanager
def directory_handle(path: Path):
    before = checked_stat(path, directory=True)
    if os.name == "nt":
        yield None
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise OSError(f"Le dossier a changé : {path}")
        yield fd
    finally:
        os.close(fd)


@contextmanager
def open_regular(path: Path, *, dir_fd=None):
    before = checked_stat(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path if dir_fd is None else path.name, flags, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as stream:
        # On Windows the path and descriptor APIs can expose different ctime values.
        if file_key(os.fstat(stream.fileno()))[:4] != file_key(before)[:4]:
            raise OSError(f"Le fichier a changé à l’ouverture : {path}")
        yield stream, before
