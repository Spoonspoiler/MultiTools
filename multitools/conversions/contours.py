from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
DEFAULT_INPUT_DIR = Path("coloriages_originaux")
DEFAULT_OUTPUT_DIR = Path("coloriages_transparents")


@dataclass(frozen=True)
class Config:
    """
    Réglages de l'extracteur de traits noirs.

    Le principe n'est PAS de dire « tout pixel sombre = noir ».
    On cherche une ENCRE NOIRE locale : un pixel doit être plus sombre que
    son voisinage dans les trois canaux R/G/B de manière relativement cohérente.
    Cela permet d'éliminer les aplats colorés, gris et blancs beaucoup mieux
    qu'un simple seuil de luminance.
    """

    # 0 = fenêtre calculée automatiquement selon la taille de l'image.
    analysis_window: int = 0

    # Détection de l'encre noire.
    min_ink: float = 0.055
    core_ink: float = 0.24
    max_channel_spread: float = 0.34
    core_channel_spread: float = 0.28

    # Secours pour les vrais noirs très profonds.
    absolute_black: int = 72
    minimum_local_contrast: float = 0.035

    # Récupère l'anti-aliasing autour du cœur du trait sans élargir le trait
    # en noir plein.
    support_radius: int = 2
    alpha_gamma: float = 0.90
    alpha_cutoff: int = 8
    core_alpha_floor: int = 205

    # Nettoyage des poussières / pixels parasites.
    min_component_size: int = 15

    # Sortie.
    crop: bool = True
    crop_padding: int = 12
    recursive: bool = True
    overwrite: bool = True
    max_size: int | None = None
    debug: bool = False


PRESETS: dict[str, Config] = {
    # Bon point de départ pour des dessins type coloriage avec contours noirs.
    "clean": Config(),

    # Plus agressif : enlève davantage de gris, textures et petits détails sombres.
    "strict": Config(
        min_ink=0.075,
        core_ink=0.30,
        max_channel_spread=0.28,
        core_channel_spread=0.23,
        absolute_black=62,
        minimum_local_contrast=0.050,
        support_radius=1,
        alpha_gamma=0.95,
        alpha_cutoff=12,
        core_alpha_floor=215,
        min_component_size=18,
    ),

    # Plus tolérant : conserve mieux les traits fins / antialiasés.
    "soft": Config(
        min_ink=0.040,
        core_ink=0.19,
        max_channel_spread=0.42,
        core_channel_spread=0.34,
        absolute_black=82,
        minimum_local_contrast=0.025,
        support_radius=2,
        alpha_gamma=0.82,
        alpha_cutoff=5,
        core_alpha_floor=190,
        min_component_size=4,
    ),
}


def ensure_odd(value: int) -> int:
    value = max(3, int(value))
    return value if value % 2 == 1 else value + 1


def automatic_window(width: int, height: int) -> int:
    """
    Fenêtre d'analyse locale.

    Elle doit être assez grande pour « voir de l'autre côté » d'un trait noir,
    mais pas assez grande pour considérer tout un vêtement sombre comme un trait.
    """
    short_side = min(width, height)
    radius = round(short_side * 0.008)
    radius = max(3, min(15, radius))
    return 2 * radius + 1


def resize_source_if_needed(image: Image.Image, max_size: int | None) -> Image.Image:
    """
    Redimensionne AVANT l'extraction.

    C'est volontaire : redimensionner un masque noir/alpha déjà extrait avec
    LANCZOS peut créer du ringing et de minuscules pixels semi-transparents autour
    des contours.
    """
    if max_size is None:
        return image

    if image.width <= max_size and image.height <= max_size:
        return image

    resized = image.copy()
    resized.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    return resized


def crop_visible_content(image: Image.Image, padding: int) -> Image.Image:
    alpha = image.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        return image

    left, top, right, bottom = bbox
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(image.width, right + padding)
    bottom = min(image.height, bottom + padding)
    return image.crop((left, top, right, bottom))


def rgba_to_white_composite_array(image: Image.Image) -> tuple[np.ndarray, np.ndarray]:
    """
    Renvoie :
      - RGB d'analyse comme si l'image était posée sur fond blanc ;
      - alpha original.

    C'est important pour les PNG transparents : un pixel transparent peut avoir
    RGB=(0,0,0) dans le fichier. Sans cette étape il serait pris pour du noir.
    """
    rgba = np.asarray(image.convert("RGBA"), dtype=np.float32)
    rgb = rgba[..., :3]
    alpha = rgba[..., 3] / 255.0

    composite = rgb * alpha[..., None] + 255.0 * (1.0 - alpha[..., None])
    return composite, alpha


def max_filter_channel(channel: np.ndarray, window: int) -> np.ndarray:
    image = Image.fromarray(np.clip(channel, 0, 255).astype(np.uint8), mode="L")
    filtered = image.filter(ImageFilter.MaxFilter(window))
    return np.asarray(filtered, dtype=np.float32)


def estimate_local_background(rgb: np.ndarray, window: int) -> np.ndarray:
    """
    Estime la couleur locale « sans l'encre noire » à l'aide d'une enveloppe
    claire (MaxFilter) sur chaque canal.
    """
    channels = [max_filter_channel(rgb[..., i], window) for i in range(3)]
    return np.stack(channels, axis=2)


def luminance_array(rgb: np.ndarray) -> np.ndarray:
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def remove_small_components(mask: np.ndarray, min_size: int) -> tuple[np.ndarray, int]:
    """
    Supprime les composantes connexes minuscules.

    Contrairement à l'ancien script, on le fait sur la VRAIE sélection de traits,
    et non sur un masque créé uniquement à partir d'un seuil de luminance.
    """
    if min_size <= 1 or not np.any(mask):
        return mask, 0

    height, width = mask.shape
    visited = np.zeros(mask.shape, dtype=np.bool_)
    cleaned = mask.copy()
    removed = 0

    # On itère seulement sur les pixels actifs, pas sur toute l'image en Python.
    active_pixels = np.argwhere(mask)

    for y, x in active_pixels:
        if visited[y, x] or not cleaned[y, x]:
            continue

        queue: deque[tuple[int, int]] = deque([(int(x), int(y))])
        visited[y, x] = True
        component: list[tuple[int, int]] = []

        while queue:
            cx, cy = queue.popleft()
            component.append((cx, cy))

            y0 = max(0, cy - 1)
            y1 = min(height - 1, cy + 1)
            x0 = max(0, cx - 1)
            x1 = min(width - 1, cx + 1)

            for ny in range(y0, y1 + 1):
                for nx in range(x0, x1 + 1):
                    if visited[ny, nx] or not cleaned[ny, nx]:
                        continue
                    visited[ny, nx] = True
                    queue.append((nx, ny))

        if len(component) < min_size:
            removed += 1
            for cx, cy in component:
                cleaned[cy, cx] = False

    return cleaned, removed


def dilate_mask(mask: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return mask

    size = 2 * radius + 1
    image = Image.fromarray((mask.astype(np.uint8) * 255), mode="L")
    dilated = image.filter(ImageFilter.MaxFilter(size))
    return np.asarray(dilated, dtype=np.uint8) > 0


def extract_black_ink(
    image: Image.Image,
    config: Config,
) -> tuple[Image.Image, dict[str, int | float], dict[str, Image.Image]]:
    """
    Extrait uniquement les traits noirs et transforme tout le reste en alpha 0.

    Idée centrale : si un trait noir est superposé à une couleur locale B,
    le pixel observé P ressemble à : P = (1-a) * B.
    Donc a ≈ (B-P)/B, et ce facteur doit être relativement cohérent sur R/G/B.

    Un aplat bleu, rose ou jaune sombre ne réduit pas R/G/B dans les mêmes
    proportions : il est donc beaucoup moins souvent confondu avec de l'encre.
    """
    source = ImageOps.exif_transpose(image).convert("RGBA")
    source = resize_source_if_needed(source, config.max_size)

    rgb, original_alpha = rgba_to_white_composite_array(source)
    height, width = original_alpha.shape

    window = config.analysis_window or automatic_window(width, height)
    window = ensure_odd(window)

    local_bg = estimate_local_background(rgb, window)

    # Fraction de baisse de chaque canal par rapport au voisinage local.
    denominator = np.maximum(local_bg, 24.0)
    darkening = np.clip((local_bg - rgb) / denominator, 0.0, 1.0)

    # Une vraie encre noire assombrit les 3 canaux de manière assez similaire.
    ink_strength = np.median(darkening, axis=2)
    channel_spread = np.max(darkening, axis=2) - np.min(darkening, axis=2)

    lum = luminance_array(rgb)
    bg_lum = luminance_array(local_bg)
    local_luma_ink = np.clip((bg_lum - lum) / np.maximum(bg_lum, 24.0), 0.0, 1.0)

    rgb_max = np.max(rgb, axis=2)
    alpha_present = original_alpha > (1.0 / 255.0)

    # Cœur fiable du trait.
    coherent_core = (
        (ink_strength >= config.core_ink)
        & (channel_spread <= config.core_channel_spread)
    )

    # Noir absolu : on le garde seulement s'il est aussi localement plus sombre.
    # Cela évite de transformer un grand aplat noir en gros bloc opaque.
    absolute_core = (
        (rgb_max <= config.absolute_black)
        & (local_luma_ink >= config.minimum_local_contrast)
    )

    core = alpha_present & (coherent_core | absolute_core)
    core, removed_core_components = remove_small_components(
        core,
        max(2, config.min_component_size // 2),
    )

    # Zone où l'on autorise l'antialiasing : très près d'un vrai trait uniquement.
    support = dilate_mask(core, config.support_radius)

    coherent_candidate = (
        (ink_strength >= config.min_ink)
        & (channel_spread <= config.max_channel_spread)
    )

    # Secours pour une bordure presque noire mais légèrement teintée par
    # l'antialiasing de l'image source.
    near_black_candidate = (
        (rgb_max <= config.absolute_black + 35)
        & (local_luma_ink >= config.minimum_local_contrast * 0.65)
    )

    candidate = alpha_present & support & (coherent_candidate | near_black_candidate)

    # Estimation d'alpha : on privilégie l'assombrissement cohérent RGB et on
    # complète avec la luminance locale pour les pixels de bord teintés.
    inferred_alpha = np.maximum(ink_strength, local_luma_ink * 0.88)
    inferred_alpha = np.clip(inferred_alpha, 0.0, 1.0)
    inferred_alpha = np.power(inferred_alpha, config.alpha_gamma)

    alpha = np.zeros((height, width), dtype=np.uint8)
    alpha_values = np.clip(np.rint(inferred_alpha * 255.0), 0, 255).astype(np.uint8)
    alpha[candidate] = alpha_values[candidate]

    # Le cœur du trait doit rester bien noir/solide, mais on ne fait PAS de grow
    # en noir plein comme dans l'ancien script.
    alpha[core] = np.maximum(alpha[core], config.core_alpha_floor)
    alpha[alpha < config.alpha_cutoff] = 0

    # Dernier nettoyage des îlots parasites sur le masque réellement exporté.
    visible = alpha > 0
    visible_clean, removed_final_components = remove_small_components(
        visible,
        config.min_component_size,
    )
    alpha[~visible_clean] = 0

    # RGB toujours noir : aucun halo rose/gris/blanc ne peut survivre dans les
    # pixels semi-transparents du contour.
    output_array = np.zeros((height, width, 4), dtype=np.uint8)
    output_array[..., 3] = alpha
    output = Image.fromarray(output_array, mode="RGBA")

    if config.crop:
        output = crop_visible_content(output, config.crop_padding)

    visible_count = int(np.count_nonzero(alpha))
    solid_count = int(np.count_nonzero(alpha >= 250))
    partial_count = int(np.count_nonzero((alpha > 0) & (alpha < 250)))

    report: dict[str, int | float] = {
        "window": window,
        "visible": visible_count,
        "solid": solid_count,
        "partial": partial_count,
        "removed_components": removed_core_components + removed_final_components,
    }

    debug_images: dict[str, Image.Image] = {}
    if config.debug:
        debug_images = {
            "core": Image.fromarray((core.astype(np.uint8) * 255), mode="L"),
            "support": Image.fromarray((support.astype(np.uint8) * 255), mode="L"),
            "ink": Image.fromarray(
                np.clip(np.rint(ink_strength * 255.0), 0, 255).astype(np.uint8),
                mode="L",
            ),
            "spread": Image.fromarray(
                np.clip(np.rint(channel_spread * 255.0), 0, 255).astype(np.uint8),
                mode="L",
            ),
            "alpha": Image.fromarray(alpha, mode="L"),
        }

    return output, report, debug_images


def find_images(input_dir: Path, recursive: bool) -> list[Path]:
    iterator = input_dir.rglob("*") if recursive else input_dir.iterdir()
    return sorted(
        path
        for path in iterator
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )


def save_debug_images(
    debug_images: dict[str, Image.Image],
    output_dir: Path,
    relative_input_path: Path,
) -> None:
    if not debug_images:
        return

    debug_dir = output_dir / "_debug" / relative_input_path.parent
    debug_dir.mkdir(parents=True, exist_ok=True)
    stem = relative_input_path.stem

    for name, image in debug_images.items():
        image.save(debug_dir / f"{stem}__{name}.png", format="PNG", optimize=True)


def process_directory(input_dir: Path, output_dir: Path, config: Config) -> None:
    if not input_dir.exists():
        raise FileNotFoundError(f"Dossier introuvable : {input_dir}")

    files = find_images(input_dir, config.recursive)
    if not files:
        print(f"Aucune image compatible trouvée dans : {input_dir.resolve()}")
        return

    success = 0
    failed = 0

    for input_path in files:
        relative_path = input_path.relative_to(input_dir)
        output_path = (output_dir / relative_path).with_suffix(".png")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if output_path.exists() and not config.overwrite:
            print(f"IGNORÉ : {output_path.name} existe déjà")
            continue

        try:
            with Image.open(input_path) as original:
                cleaned, report, debug_images = extract_black_ink(original, config)

            cleaned.save(output_path, format="PNG", optimize=True)
            save_debug_images(debug_images, output_dir, relative_path)
            success += 1

            print(
                f"OK : {input_path.name} -> {output_path} | "
                f"fenêtre={report['window']} | "
                f"pixels={report['visible']} | "
                f"semi-alpha={report['partial']} | "
                f"îlots supprimés={report['removed_components']}"
            )
        except Exception as error:
            failed += 1
            print(f"ERREUR : {input_path} | {error}")

    print()
    print(f"Terminé : {success} réussite(s), {failed} erreur(s).")


def build_config(arguments: argparse.Namespace) -> Config:
    config = PRESETS[arguments.preset]

    updates: dict[str, object] = {
        "crop": not arguments.no_crop,
        "crop_padding": max(0, arguments.padding),
        "recursive": not arguments.no_recursive,
        "overwrite": not arguments.no_overwrite,
        "max_size": arguments.max_size,
        "debug": arguments.debug,
    }

    if arguments.window is not None:
        updates["analysis_window"] = 0 if arguments.window == 0 else ensure_odd(arguments.window)
    if arguments.support is not None:
        updates["support_radius"] = max(0, arguments.support)
    if arguments.min_component is not None:
        updates["min_component_size"] = max(1, arguments.min_component)
    if arguments.ink is not None:
        updates["min_ink"] = max(0.0, min(1.0, arguments.ink))
    if arguments.core_ink is not None:
        updates["core_ink"] = max(0.0, min(1.0, arguments.core_ink))
    if arguments.spread is not None:
        updates["max_channel_spread"] = max(0.0, min(1.0, arguments.spread))

    return replace(config, **updates)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extrait hors ligne les contours noirs d'illustrations et rend "
            "tout le reste transparent."
        )
    )

    parser.add_argument(
        "input",
        nargs="?",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help="Dossier source (défaut: coloriages_originaux).",
    )
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Dossier de sortie (défaut: coloriages_transparents).",
    )
    parser.add_argument(
        "--preset",
        choices=tuple(PRESETS),
        default="clean",
        help="clean = recommandé, strict = enlève plus, soft = garde plus de détails.",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=None,
        help="Fenêtre locale impaire. 0 = automatique. Ex.: 11, 15, 21.",
    )
    parser.add_argument(
        "--support",
        type=int,
        default=None,
        help="Rayon en pixels pour récupérer l'antialiasing autour du trait.",
    )
    parser.add_argument(
        "--min-component",
        type=int,
        default=None,
        help="Taille minimale d'un îlot de pixels conservé.",
    )
    parser.add_argument(
        "--ink",
        type=float,
        default=None,
        help="Seuil faible d'encre noire, entre 0 et 1. Plus haut = plus strict.",
    )
    parser.add_argument(
        "--core-ink",
        type=float,
        default=None,
        help="Seuil du cœur du trait, entre 0 et 1.",
    )
    parser.add_argument(
        "--spread",
        type=float,
        default=None,
        help="Tolérance de différence entre R/G/B, entre 0 et 1. Plus bas = plus strict.",
    )
    parser.add_argument("--padding", type=int, default=12, help="Marge du recadrage.")
    parser.add_argument("--no-crop", action="store_true", help="Désactive le recadrage.")
    parser.add_argument("--no-recursive", action="store_true", help="Ignore les sous-dossiers.")
    parser.add_argument("--no-overwrite", action="store_true", help="Ne remplace pas les PNG existants.")
    parser.add_argument("--max-size", type=int, default=None, help="Réduit la source avant extraction. Ex.: 1024.")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Sauve les masques de diagnostic dans output/_debug/.",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    config = build_config(arguments)
    process_directory(arguments.input, arguments.output, config)


if __name__ == "__main__":
    main()