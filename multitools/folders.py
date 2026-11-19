"""Créer un seul dossier nommé, sans remplacer un élément existant."""
import os
from pathlib import Path


def create_named_folder(parent,name):
    name=name.strip()
    if not name or name in {'.','..'} or any(c in name for c in '/\\') or any(ord(c)<32 for c in name):
        raise ValueError('Indique un nom de dossier seul, sans chemin ni caractères de contrôle.')
    parent=Path(parent).resolve(strict=True)
    if not parent.is_dir(): raise NotADirectoryError(str(parent))
    if os.mkdir in os.supports_dir_fd:
        descriptor=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: os.mkdir(name,mode=0o755,dir_fd=descriptor)
        finally: os.close(descriptor)
    else:
        (parent/name).mkdir()
    return parent/name
