"""Prévisualisations bornées et opérations autorisées, sans exécution de code IA."""
import numpy as np
from PIL import Image, ImageOps, ImageEnhance, ImageFilter

TOOLS = ('luminosite', 'contraste', 'saturation', 'gris', 'inversion', 'seuil',
         'rouge', 'contours', 'bruit', 'accentuation', 'dilatation', 'erosion',
         'ouverture', 'fermeture', 'rotation', 'niveaux')


def preview(path, bound=1800):
    with Image.open(path) as source:
        size = ImageOps.exif_transpose(source).size
        source.draft('RGB', (bound, bound))
        image = ImageOps.exif_transpose(source).convert('RGB')
        image.thumbnail((bound, bound), Image.Resampling.LANCZOS)
    return image, size


def operation(image, command):
    if not isinstance(command, dict) or set(command) - {'tool', 'value'}:
        raise ValueError('Commande invalide.')
    tool, value = command.get('tool'), command.get('value', 1)
    if tool not in TOOLS or type(value) not in (int, float) or not np.isfinite(value):
        raise ValueError('Outil ou paramètre non autorisé.')
    if tool in ('luminosite', 'contraste', 'saturation'):
        if not 0 <= value <= 4: raise ValueError('Facteur attendu entre 0 et 4.')
        cls = {'luminosite': ImageEnhance.Brightness, 'contraste': ImageEnhance.Contrast,
               'saturation': ImageEnhance.Color}[tool]
        return cls(image).enhance(value)
    if tool == 'rotation':
        raise ValueError('Rotation différée : nécessite la transformation des géométries.')
    if tool == 'gris': return ImageOps.grayscale(image).convert('RGB')
    if tool == 'inversion': return ImageOps.invert(image.convert('RGB'))
    if tool == 'niveaux': return ImageOps.autocontrast(image)
    if tool == 'seuil':
        if not 0 <= value <= 255: raise ValueError('Seuil attendu entre 0 et 255.')
        return image.convert('L').point(lambda p: 255 if p >= value else 0).convert('RGB')
    if tool == 'rouge':
        a = np.asarray(image.convert('RGB')).astype(float)
        mask = (a[:,:,0] > a[:,:,1] * 1.25) & (a[:,:,0] > a[:,:,2] * 1.25)
        return Image.fromarray((mask * 255).astype('uint8')).convert('RGB')
    filters = {'contours': ImageFilter.FIND_EDGES, 'bruit': ImageFilter.MedianFilter(3),
               'accentuation': ImageFilter.SHARPEN, 'dilatation': ImageFilter.MaxFilter(3),
               'erosion': ImageFilter.MinFilter(3)}
    if tool in filters: return image.filter(filters[tool])
    steps = (ImageFilter.MinFilter(3), ImageFilter.MaxFilter(3))
    if tool == 'fermeture': steps = steps[::-1]
    return image.filter(steps[0]).filter(steps[1])


def overlay(base, other, *, opacity=.5, x=0, y=0, scale=1, angle=0):
    """Alignement manuel sur prévisualisation, sans toucher aux fichiers sources."""
    if not 0 <= opacity <= 1 or not .1 <= scale <= 4 or not -180 <= angle <= 180:
        raise ValueError('Paramètres de superposition invalides.')
    moved = other.resize((max(1,int(other.width*scale)),max(1,int(other.height*scale))))
    moved = moved.rotate(angle, expand=True).convert('RGBA')
    layer = Image.new('RGBA',base.size)
    layer.paste(moved,(int(x),int(y)))
    return Image.blend(base.convert('RGBA'),layer,opacity).convert('RGB')
