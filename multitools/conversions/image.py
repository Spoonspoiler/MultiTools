from pathlib import Path
from PIL import Image, ImageOps, features


FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
    ".avif": "AVIF",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".bmp": "BMP",
    ".gif": "GIF",
    ".ico": "ICO",
}


def _on_white(image: Image.Image) -> Image.Image:
    if not image.has_transparency_data:
        return image.convert("RGB")
    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, "white")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def convert_image(input_path, output_path):
    input_path = Path(input_path)
    output_path = Path(output_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Fichier introuvable : {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    ext = output_path.suffix.lower()
    format_name = FORMATS.get(ext)
    if format_name is None:
        raise ValueError(f"Format image non supporté : {ext}")
    if format_name == "AVIF" and not features.check("avif"):
        raise ValueError("L’encodage AVIF est indisponible avec cette installation de Pillow.")

    with Image.open(input_path) as original:
        image = ImageOps.exif_transpose(original)
        options = {}
        if format_name == "JPEG":
            if image.has_transparency_data or image.mode not in {"L", "RGB", "CMYK"}:
                image = _on_white(image)
            options = {"quality": 95}
        elif format_name == "PNG" and image.mode in {"CMYK", "F"}:
            image = image.convert("RGB")
        elif format_name == "WEBP":
            options = {"quality": 95}
        elif format_name == "AVIF":
            options = {"quality": 80, "speed": 8}
        elif format_name == "TIFF":
            options = {"compression": "tiff_adobe_deflate"}
        elif format_name == "BMP":
            image = _on_white(image)
        elif format_name == "ICO":
            image = ImageOps.pad(image.convert("RGBA"), (256, 256), Image.Resampling.LANCZOS)
            options = {"sizes": [(size, size) for size in (16, 32, 48, 64, 128, 256)]}
        image.save(output_path, format_name, **options)

    return output_path
