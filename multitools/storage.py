"""Rôles de stockage et vérifications avant une écriture volumineuse."""

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys


ROLES = ("SYSTEM", "DATA", "BACKUP")


@dataclass(frozen=True)
class Mount:
    path: Path
    uuid: str
    disks: frozenset[str]
    device: str


def parse_mounts(data: dict) -> list[Mount]:
    mounts = {}
    def visit(node, parents):
        name = node.get("name", "")
        disks = parents | {name} if node.get("type") == "disk" and name else parents
        for mount in node.get("mountpoints") or ():
            if not mount:
                continue
            previous = mounts.get(mount)
            ancestry = disks | (previous.disks if previous else set())
            mounts[mount] = Mount(Path(mount), node.get("uuid") or "", frozenset(ancestry), name)
        for child in node.get("children", []):
            visit(child, disks)
    for node in data.get("blockdevices", []):
        visit(node, set())
    return sorted(mounts.values(), key=lambda item: str(item.path))


def mounts() -> list[Mount]:
    if sys.platform.startswith("linux") and (lsblk := shutil.which("lsblk")):
        result = subprocess.run([lsblk, "--json", "--paths", "--output", "NAME,TYPE,UUID,MOUNTPOINTS"],
                                capture_output=True, text=True, check=True, timeout=5)
        return parse_mounts(json.loads(result.stdout))
    if sys.platform == "win32":
        import os
        return [Mount(Path(root), "", frozenset(), "") for root in os.listdrives()]
    return []


def verify_mount(spec, inventory: list[Mount]) -> Mount:
    target = Path(spec["path"])
    for mount in inventory:
        if mount.path == target and (not spec.get("uuid") or spec["uuid"] == mount.uuid):
            return mount
    raise OSError(f"Point de montage absent ou disque différent · {target}")


def require_space(spec, inventory: list[Mount], byte_count: int):
    mount = verify_mount(spec, inventory)
    free = shutil.disk_usage(mount.path).free
    if byte_count > free:
        raise OSError(f"Espace insuffisant sur {mount.path} · {free:,} octets disponibles.")
    return mount


def backup_independence(data: Mount, backup: Mount) -> str:
    if data.path == backup.path or data.disks & backup.disks:
        return "Disque partagé avec DATA"
    if data.disks and backup.disks and data.uuid and backup.uuid and data.uuid != backup.uuid:
        return "Disque physique distinct selon lsblk"
    return "Indépendance physique non vérifiée"


def guard_target(target: Path, byte_count: int, roles: dict) -> None:
    matches = [spec for role, spec in roles.items() if role in ROLES and target.is_relative_to(Path(spec["path"]))]
    if matches:
        spec = max(matches, key=lambda item: len(Path(item["path"]).parts))
        if sys.platform.startswith("linux") and not spec.get("uuid"):
            raise OSError(f"UUID du volume attendu inconnu. Configure à nouveau son rôle · {spec['path']}")
        try:
            require_space(spec, mounts(), byte_count)
        except (subprocess.SubprocessError, ValueError) as error:
            raise OSError(f"Impossible de contrôler le montage · {spec['path']}") from error


@dataclass(frozen=True)
class BackupPlan:
    source: Path
    target: Path
    backend: str


def plan_backup(roles: dict, inventory: list[Mount], backend: str) -> BackupPlan:
    if backend not in {"restic", "kopia"}:
        raise ValueError("Moteur de sauvegarde inconnu.")
    data, backup = (verify_mount(roles[role], inventory) for role in ("DATA", "BACKUP"))
    if not data.disks or not backup.disks or data.disks & backup.disks or not data.uuid or not backup.uuid or data.uuid == backup.uuid:
        raise OSError("La destination BACKUP doit être un disque physique distinct de DATA.")
    return BackupPlan(data.path, backup.path, backend)
