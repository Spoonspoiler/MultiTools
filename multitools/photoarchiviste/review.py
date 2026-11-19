from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .inventory import Comparison, Gate, Photo, comparisons
from .journal import Archive, Choice, DEFAULT_CATEGORIES, Transfer


class Decision(Enum):
    KEEP_LEFT = "Garder la gauche"
    KEEP_RIGHT = "Garder la droite"
    KEEP_BOTH = "Garder les deux"
    REJECT_BOTH = "Ne garder aucune"


def photo_format(photo: Photo) -> str:
    name = (photo.format or photo.path.suffix.lstrip('.') or 'INCONNU').upper()
    return {'JPG': 'JPEG', 'JPE': 'JPEG', 'TIF': 'TIFF'}.get(name, name)


def filter_cases(cases: list[Comparison], format_name: str) -> list[Comparison]:
    """Conserver le groupe entier pour ne pas cacher les autres variantes."""
    if format_name == 'Tous les formats':
        return cases
    return [case for case in cases if any(photo_format(photo) == format_name for photo in case.photos)]


def propose(archive: Archive, decision: Decision, left: Photo, right: Photo | None,
            left_category: str, right_category: str, reason: str, *, move: bool, by_year: bool) -> list[Transfer]:
    if right is None and decision not in {Decision.KEEP_LEFT, Decision.REJECT_BOTH}:
        raise ValueError("Cette décision exige deux photos.")
    choices = [Choice(left, left_category if decision in {Decision.KEEP_LEFT, Decision.KEEP_BOTH} else None)]
    if right is not None:
        category = right_category if decision in {Decision.KEEP_RIGHT, Decision.KEEP_BOTH} else None
        choices.append(Choice(right, category))
    label = decision.value if right else ("Classer" if decision == Decision.KEEP_LEFT else "Copier dans À jeter")
    return archive.plan(choices, label, reason, move=move, by_year=by_year)


def propose_selection(archive: Archive, photos: tuple[Photo, ...], category: str, reason: str, *,
                      move: bool, by_year: bool, gate: Gate) -> list[Transfer]:
    return archive.plan([Choice(photo, category) for photo in photos], "Garder la sélection", reason,
                        move=move, by_year=by_year, gate=gate)


@dataclass(frozen=True)
class Snapshot:
    roots: list[Path]
    photos: list[Photo]
    cases: dict[str, list[Comparison]]
    transfers: list[Transfer]
    categories: tuple[str, ...]
    scan_incomplete: bool


def snapshot(archive: Archive, gate: Gate, *, include_visual: bool = True) -> Snapshot:
    photos = archive.photos()
    transfers = archive.transfers()
    categories = tuple(dict.fromkeys((*DEFAULT_CATEGORIES, *(item.category for item in transfers if item.category))))
    return Snapshot(archive.roots, photos, comparisons(photos, archive.withheld(), gate, include_visual=include_visual),
                    transfers, categories, archive.scan_incomplete)
