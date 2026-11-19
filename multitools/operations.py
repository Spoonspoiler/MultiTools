from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

import numpy as np
from PIL import Image, ImageOps

from .conversions.contours import Config, extract_black_ink
from .conversions.image import convert_image
from .provenance import digest, link


@dataclass(frozen=True)
class Operation:
    label: str
    extension: str
    description: str
    suffix: str = ""


TRANSPARENT_CONTOURS = Operation("Contours transparents", ".png", "Extrait les traits noirs d’un dessin sur fond transparent.", "-contours")
SVG_SILHOUETTE = Operation("Silhouette SVG", ".svg", "Crée une silhouette vectorielle à partir des pixels sombres d’une image.")

IMAGES = (
    Operation("PNG", ".png", "Image sans perte avec transparence si la source en contient."),
    Operation("JPEG", ".jpg", "Image compressée. Les zones transparentes deviennent blanches."),
    Operation("WebP", ".webp", "Image compressée, avec prise en charge de la transparence."),
    Operation("AVIF", ".avif", "Image compressée avec prise en charge de la transparence."),
    Operation("TIFF", ".tiff", "Image compressée sans perte, adaptée aux scans et aux retouches."),
    Operation("BMP", ".bmp", "Image bitmap. Les zones transparentes deviennent blanches."),
    Operation("GIF · image fixe", ".gif", "Une seule image, avec une palette de 256 couleurs au maximum."),
    Operation("ICO", ".ico", "Icône carrée avec fond transparent et tailles de 16 à 256 pixels."),
    TRANSPARENT_CONTOURS,
    SVG_SILHOUETTE,
)

MOV_COPY = Operation("MOV · copie des pistes", ".mov", "Change de conteneur en conservant les pistes vidéo et audio compatibles.")
MKV_COPY = Operation("MKV · copie des pistes", ".mkv", "Place les pistes vidéo et audio compatibles dans un fichier Matroska sans réencodage.")

MEDIA = (
    Operation("MP4 · H.264", ".mp4", "Réencode la vidéo en H.264 et la première piste audio en AAC."),
    Operation("MOV · ProRes 422 HQ", ".mov", "Réencode en ProRes pour le montage. Le fichier peut être volumineux."),
    MOV_COPY,
    Operation("WebM · VP9", ".webm", "Réencode la vidéo en VP9 et la première piste audio en Opus."),
    MKV_COPY,
    Operation("MP3 · extraire l’audio", ".mp3", "Extrait la première piste audio en MP3 stéréo."),
    Operation("WAV · extraire l’audio", ".wav", "Extrait la première piste audio en WAV stéréo non compressé."),
    Operation("FLAC · extraire l’audio", ".flac", "Compresse la première piste en FLAC, à la fréquence d’origine."),
    Operation("M4A · AAC", ".m4a", "Convertit la première piste audio en AAC stéréo."),
    Operation("Ogg · Vorbis", ".ogg", "Convertit la première piste audio en Vorbis stéréo."),
    Operation("Opus", ".opus", "Convertit la première piste audio en Opus stéréo dans un conteneur Ogg."),
)


class ConversionCancelled(Exception):
    pass


def check_cancelled(cancelled: Event) -> None:
    if cancelled.is_set():
        raise ConversionCancelled


def _write_silhouette(source: Path, output: Path, cancelled: Event) -> None:
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGBA")
        background = Image.new("RGBA", image.size, "white")
        mask = np.asarray(Image.alpha_composite(background, image).convert("L")) < 128
    width, height = image.size

    # Consecutive rows with the same dark run share one rectangle in the path.
    active: dict[tuple[int, int], int] = {}
    with output.open("w", encoding="utf-8") as svg:
        svg.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                  f'viewBox="0 0 {width} {height}">\n<path d="')
        for y in range(height + 1):
            check_cancelled(cancelled)
            transitions = np.flatnonzero(np.diff(mask[y], prepend=False, append=False)) if y < height else ()
            runs = set(zip(transitions[::2], transitions[1::2]))
            for x0, x1 in sorted(active.keys() - runs):
                top = active.pop((x0, x1))
                svg.write(f"M{x0} {top}h{x1 - x0}v{y - top}h{x0 - x1}z")
            for run in runs:
                active.setdefault(run, y)
        svg.write('"/>\n</svg>\n')


def _publish(temporary: Path, directory: Path, stem: str) -> Path:
    number = 1
    while True:
        name = stem if number == 1 else f"{stem} ({number})"
        target = directory / f"{name}{temporary.suffix}"
        try:
            if os.name == "nt":
                temporary.rename(target)
            else:
                target.hardlink_to(temporary)
        except FileExistsError:
            number += 1
        else:
            return target


def convert_file(source: Path, directory: Path, operation: Operation, cancelled: Event) -> Path:
    check_cancelled(cancelled)
    if not source.is_file():
        raise FileNotFoundError(f"Fichier introuvable : {source}")
    if not directory.is_dir():
        raise NotADirectoryError(f"Dossier de sortie introuvable : {directory}")

    source_sha = digest(source, lambda: check_cancelled(cancelled)) if operation in IMAGES else None
    with TemporaryDirectory(prefix=".multitools-", dir=directory) as workdir:
        temporary = Path(workdir) / f"sortie{operation.extension}"
        if operation == TRANSPARENT_CONTOURS:
            with Image.open(source) as original:
                image, _, _ = extract_black_ink(original, Config(crop=False))
                image.save(temporary, "PNG")
        elif operation == SVG_SILHOUETTE:
            _write_silhouette(source, temporary, cancelled)
        elif operation in IMAGES:
            convert_image(source, temporary)
        else:
            from .media import convert_media

            convert_media(source, temporary, operation, cancelled)

        check_cancelled(cancelled)
        if source_sha and digest(source, lambda: check_cancelled(cancelled)) != source_sha:
            raise OSError('La source a changé pendant la conversion.')
        target = _publish(temporary, directory, source.stem + operation.suffix)
        if source_sha:
            try:
                link(source, target, operation.label, source_sha=source_sha)
            except Exception:
                target.unlink()
                raise
        return target
