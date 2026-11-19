"""Règles communes des tags par photo ; aucune unicité entre photos."""
import unicodedata

MAX_TAGS = 4


def clean(values):
    result = {}
    for value in values:
        if not isinstance(value, str): raise ValueError('Un tag doit être un texte.')
        value = unicodedata.normalize('NFC', value).strip()[:40]
        if value: result.setdefault(value.casefold(), value)
    return result


def validate(values):
    if not isinstance(values, list): raise ValueError('Les tags doivent être une liste de textes.')
    tags = list(clean(values).values())
    if len(tags) > MAX_TAGS: raise ValueError('4 tags maximum par photo.')
    return tags
