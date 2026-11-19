from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import os
from pathlib import Path
from threading import Event

import numpy as np
from PIL import Image, ImageOps, ImageStat

from .path_safety import checked_stat

EXTENSIONS = frozenset(".jpg .jpeg .png .webp .tif .tiff .bmp .gif .avif .jpe .ico".split())
BLOCK_SIZE = 1024 * 1024
VISUAL_DISTANCE = 4
_DCT = np.cos(np.pi / 32 * np.arange(8)[:, None] * (np.arange(32) + 0.5))
_DCT[0] /= np.sqrt(2)


class TaskStopped(Exception):
    pass


class Gate:
    def __init__(self) -> None:
        self.stopped = Event()
        self.running = Event()
        self.running.set()

    def check(self) -> None:
        while not self.stopped.is_set():
            if self.running.wait(0.15):
                if not self.stopped.is_set():
                    return
                break
        raise TaskStopped


@dataclass(frozen=True)
class Photo:
    path: Path
    size: int
    mtime_ns: int
    sha256: str
    width: int = 0
    height: int = 0
    taken: str = ""
    format: str = ""
    camera: str = ""
    visual: str = ""
    color: str = ""
    warning: str = ""
    deferred: bool = False


@dataclass(frozen=True)
class Comparison:
    photos: tuple[Photo, ...]
    reason: str


def digest_file(path: Path, gate: Gate) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(BLOCK_SIZE):
            gate.check()
            digest.update(block)
    return digest.hexdigest()


def stat_key(stat: os.stat_result) -> tuple[int, int, int, int]:
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _image_details(image: Image.Image) -> dict:
    exif = image.getexif()
    detail = exif.get_ifd(34665)
    taken = detail.get(36867) or exif.get(36867)
    try:
        taken = datetime.strptime(str(taken), "%Y:%m:%d %H:%M:%S").isoformat(sep=" ")
    except ValueError:
        taken = ""
    camera = " ".join(str(exif[tag]).strip() for tag in (271, 272) if exif.get(tag))
    width, height = image.size
    if exif.get(274) in (5, 6, 7, 8):
        width, height = height, width

    image.draft("RGB", (128, 128))
    thumb = ImageOps.exif_transpose(image).convert("RGBA")
    thumb.thumbnail((96, 96), Image.Resampling.LANCZOS)
    background = Image.new("RGBA", thumb.size, "white")
    rgb = Image.alpha_composite(background, thumb).convert("RGB")
    rgb_stat = ImageStat.Stat(rgb)
    gray = np.asarray(rgb.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float64)
    visual = ""
    if gray.std() >= 2:
        low_frequencies = (_DCT @ gray @ _DCT.T).ravel()[1:]
        bits = low_frequencies > np.median(low_frequencies)
        signature = int("".join("1" if bit else "0" for bit in bits), 2)
        visual = f"p{signature:016x}"
    return {"taken": taken, "camera": camera[:200], "width": width, "height": height,
            "visual": visual, "color": ",".join(str(round(mean)) for mean in rgb_stat.mean)}


def inspect_photo(path: Path, gate: Gate) -> Photo:
    before = path.stat()
    sha256 = digest_file(path, gate)
    try:
        with Image.open(path) as image:
            details = {"format": image.format or "", **_image_details(image)}
    except Exception as error:
        details = {"warning": str(error) or type(error).__name__}
    if stat_key(before) != stat_key(path.stat()):
        raise OSError("Le fichier a changé pendant l’analyse. Relance l’indexation.")
    return Photo(path, before.st_size, before.st_mtime_ns, sha256, **details)


def unique_roots(roots: list[Path], workspace: Path) -> list[Path]:
    unique = sorted({root.absolute() for root in roots}, key=lambda path: (len(path.parts), str(path)))
    output_roots = [workspace / folder for folder in ("TRIEES", "A_JETER")]
    accepted = []
    for root in unique:
        checked_stat(root, directory=True)
        if any(root.is_relative_to(output) for output in output_roots):
            raise ValueError("Les dossiers TRIEES et A_JETER sont réservés aux résultats.")
        if not any(root.is_relative_to(parent) for parent in accepted):
            accepted.append(root)
    return accepted


def walk_photos(root: Path, workspace: Path, on_error):
    excluded = {workspace / name for name in ("TRIEES", "A_JETER")}
    for folder, subfolders, files in os.walk(root, onerror=on_error, followlinks=False):
        directory = Path(folder)
        if (directory / '.multitools-journal').is_dir():
            subfolders[:] = []
            continue
        subfolders[:] = sorted(name for name in subfolders if name != '.multitools-quarantaine' and directory / name not in excluded
                               and not (directory / name).is_symlink() and not (directory / name).is_junction())
        for name in sorted(files):
            path = directory / name
            if path.suffix.lower() in EXTENSIONS and not path.is_symlink() and path.is_file():
                yield path


def visual_distance(left: Photo, right: Photo) -> int:
    return (int(left.visual.removeprefix("p"), 16) ^ int(right.visual.removeprefix("p"), 16)).bit_count()


def _plausible_visual(left: Photo, right: Photo) -> bool:
    if left.sha256 == right.sha256:
        return False
    ratio = left.width * right.height / (left.height * right.width)
    delta = sum(abs(int(a) - int(b)) for a, b in zip(left.color.split(","), right.color.split(",")))
    return 0.80 <= ratio <= 1.25 and delta <= 90


def _visual_series(groups: list[list[Photo]], gate: Gate) -> list[list[list[Photo]]]:
    parents = list(range(len(groups)))
    anchors: dict[int, Photo] = {}
    bands = defaultdict(list)

    def root(index: int) -> int:
        while index != parents[index]:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for index, group in enumerate(groups):
        gate.check()
        anchor = next((photo for photo in group if photo.visual), None)
        if anchor is None:
            continue
        anchors[index] = anchor
        method = anchor.visual.startswith("p")
        signature = int(anchor.visual.removeprefix("p"), 16)
        # A distance of at most four bits shares one of these five disjoint bands.
        keys = [(method, offset, (signature >> offset) & 0x1FFF) for offset in range(0, 64, 13)]
        candidates = {other for key in keys for other in bands[key]}
        for other in candidates:
            gate.check()
            left, right = root(index), root(other)
            if left != right and visual_distance(anchor, anchors[other]) <= VISUAL_DISTANCE and _plausible_visual(anchor, anchors[other]):
                parents[max(left, right)] = min(left, right)
        for key in keys:
            bands[key].append(index)

    series = defaultdict(list)
    for index, group in enumerate(groups):
        series[root(index)].append(group)
    return list(series.values())


def comparisons(photos: list[Photo], withheld: set[Path], gate: Gate, *, include_visual: bool = True) -> dict[str, list[Comparison]]:
    exact = defaultdict(list)
    for photo in photos:
        gate.check()
        exact[(photo.size, photo.sha256)].append(photo)

    results = {name: [] for name in ("Exacts", "Proches", "Toutes", "Reportées")}
    def is_available(photo: Photo) -> bool:
        return photo.path not in withheld and not photo.deferred

    for group in exact.values():
        gate.check()
        active = tuple(item for item in group if is_available(item))
        if len(group) > 1 and active:
            reason = f"{len(group)} fichiers · même taille et même SHA-256 à l’indexation"
            if len(active) == 1:
                reason += " · dernière variante à examiner"
            results["Exacts"].append(Comparison(active, reason))
        for photo in group:
            if photo.path not in withheld:
                mode = "Reportées" if photo.deferred else "Toutes"
                results[mode].append(Comparison((photo,), "Examen individuel"))
    if include_visual:
        for series in _visual_series(list(exact.values()), gate):
            gate.check()
            total = sum(len(group) for group in series)
            active = [photo for group in series for photo in group if is_available(photo)]
            if total < 2 or not active:
                continue
            if len(series) == 1:
                reason = "Doublons exacts · même taille et même SHA-256 à l’indexation"
            else:
                reason = f"Série de ressemblances · {len(series)} versions distinctes"
            reason += f" · {len(active)} sur {total} à examiner"
            active.sort(key=lambda photo: (not photo.taken, photo.taken, photo.path.name.casefold(), str(photo.path)))
            results["Proches"].append(Comparison(tuple(active), reason))
    return results
