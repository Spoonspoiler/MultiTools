"""Modèle commun aux formulaires, géométries et exemples, sans dépendance Tk."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

FIELDS = ('Type de document', 'Titre original', 'Localité', 'Commune', 'Département ou région',
          'Pays', 'Date de création', 'Date des relevés', 'Date des modifications',
          'Période représentée', 'Auteur ou géomètre', 'Organisme producteur',
          'Technique', 'Support original', 'Échelle', 'Référence archivistique',
          'Source documentaire', 'Description générale', 'Observations')
LAYERS = {'cadastre': '#ff4040', 'eau': '#3388ff', 'limite 1': '#ffff00',
          'limite 2': '#ff80c0', 'ignorer': '#b060ff'}
SKILL = {'id': 'cadastre', 'fields': FIELDS, 'layers': LAYERS,
         'instructions': 'Lire une carte historique. Ne rien inventer. Une information inconnue reste vide. '
                         'Signaler toute date incertaine. Distinguer visible et hypothese.'}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def document(path):
    path = Path(path).resolve()
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return {'id': uuid4().hex, 'path': str(path), 'sha256': digest.hexdigest(),
            'skill': SKILL['id'], 'metadata': {k: {'value': '', 'status': 'indetermine',
            'justification': '', 'region': None} for k in FIELDS}, 'objects': [],
            'layers': dict(LAYERS), 'hidden': [], 'transforms': [], 'corrections': [],
            'history': [], 'relations': [], 'comparison': {}, 'territory': ''}


def geometry(kind, points, category='cadastre'):
    minimum = {'line': 2, 'polyline': 2, 'polygon': 3}.get(kind)
    if minimum is None or len(points) < minimum:
        raise ValueError('Nombre de sommets insuffisant.')
    import math
    if any(len(p) != 2 or any(not math.isfinite(float(v)) for v in p) for p in points):
        raise ValueError('Coordonnées invalides.')
    return {'id': uuid4().hex, 'kind': kind, 'points': [list(p) for p in points],
            'category': category, 'number': '', 'section': '', 'locality': '',
            'comment': '', 'transcription': '', 'source': 'humain', 'status': 'corrige',
            'exclusion': 'ignore' if category == 'ignorer' else '', 'width': 2}


class Editing:
    def __init__(self, data):
        self.data, self.undo_stack, self.redo_stack = data, [], []

    def change(self, action, mutate):
        before = deepcopy(self.data)
        mutate(self.data)
        self.undo_stack.append(before)
        self.redo_stack.clear()
        self.data['history'].append({'date': stamp(), 'action': action})

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(deepcopy(self.data))
            self.data = self.undo_stack.pop()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(deepcopy(self.data))
            self.data = self.redo_stack.pop()

    def correct(self, target, before, after, category='', validated=False):
        self.data['corrections'].append({'id': uuid4().hex, 'document': self.data['id'],
            'target': target, 'initial': deepcopy(before), 'final': deepcopy(after),
            'category': category, 'date': stamp(), 'contributor': '',
            'status': 'valide' if validated else 'corrige'})


class Store:
    """Tables supplémentaires dans documents.sqlite3 ; aucun original n'est écrit."""
    def __init__(self, database):
        self.path = Path(database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._open() as db:
            db.execute('CREATE TABLE IF NOT EXISTS visual_documents(id TEXT PRIMARY KEY, path TEXT, data TEXT)')

    @contextmanager
    def _open(self):
        db = sqlite3.connect(self.path)
        try:
            with db: yield db
        finally:
            db.close()

    def save(self, data):
        with self._open() as db:
            db.execute('INSERT OR REPLACE INTO visual_documents VALUES(?,?,?)',
                       (data['id'], data['path'], json.dumps(data, ensure_ascii=False, allow_nan=False)))

    def load(self, identifier):
        with self._open() as db:
            row = db.execute('SELECT data FROM visual_documents WHERE id=?', (identifier,)).fetchone()
        if row is None: raise ValueError('Document absent.')
        return json.loads(row[0])

    def listing(self):
        with self._open() as db:
            return db.execute('SELECT id,path FROM visual_documents ORDER BY rowid DESC').fetchall()

    def examples(self, category='', limit=5):
        found = []
        for identifier, _ in self.listing():
            for item in self.load(identifier)['corrections']:
                if item['status'] == 'valide' and (not category or item['category'] == category):
                    found.append(item)
        return found[-limit:]


def validate_analysis(data):
    if not isinstance(data, dict): raise ValueError('Réponse documentaire invalide.')
    for name, value in data.items():
        if name not in FIELDS or not isinstance(value, dict) or set(value)-{'value','status','justification'}:
            raise ValueError('Champ IA invalide.')
        if not isinstance(value.get('value'), str) or len(value['value']) > 4000:
            raise ValueError('Valeur IA invalide.')
        if value.get('status') not in ('visible', 'hypothese'):
            raise ValueError('Provenance IA invalide.')
        if not isinstance(value.get('justification', ''), str):
            raise ValueError('Justification invalide.')
    return data
