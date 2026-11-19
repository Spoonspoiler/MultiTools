from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from multitools.photoarchiviste.exact_duplicates import audit, quarantine, restore, QUARANTINE
from multitools.photoarchiviste.inventory import Gate, TaskStopped, walk_photos


class ExactDuplicateTests(unittest.TestCase):
    def setUp(self):
        # Volume local réel, uniquement des données synthétiques dans un dossier temporaire.
        self.temp = tempfile.TemporaryDirectory(prefix='multitools-test-', dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def seed(self):
        for name in ['a.jpg', 'b.jpg', 'c.doc']:
            (self.root / name).write_bytes(b'contenu identique')
        (self.root / 'unique.doc').write_bytes(b'autre document')
        return audit([self.root], Gate())

    def test_keeps_one_of_three_and_every_distinct_document_and_restores(self):
        plan = self.seed()
        self.assertEqual(len(plan), 2)
        manifests = [quarantine(action, Gate()) for action in plan]
        self.assertEqual((self.root / 'a.jpg').read_bytes(), b'contenu identique')
        self.assertEqual((self.root / 'unique.doc').read_bytes(), b'autre document')
        self.assertFalse((self.root / 'b.jpg').exists())
        self.assertFalse((self.root / 'c.doc').exists())
        self.assertEqual(audit([self.root], Gate()), [])
        for manifest in manifests:
            restored = restore(manifest, Gate())
            self.assertEqual(restored.read_bytes(), b'contenu identique')

    def test_changed_or_missing_keeper_prevents_move(self):
        plan = self.seed()
        plan[0].keep.path.unlink()
        with self.assertRaises(OSError):
            quarantine(plan[0], Gate())
        self.assertTrue(plan[0].extra.path.exists())

    def test_changed_duplicate_prevents_move(self):
        plan = self.seed()
        plan[0].extra.path.write_bytes(b'nouveau contenu')
        with self.assertRaises(OSError):
            quarantine(plan[0], Gate())
        self.assertEqual(plan[0].extra.path.read_bytes(), b'nouveau contenu')

    def test_restore_never_overwrites(self):
        action = self.seed()[0]
        manifest = quarantine(action, Gate())
        action.extra.path.write_bytes(b'fichier reapparu')
        with self.assertRaises(OSError):
            restore(manifest, Gate())
        self.assertEqual(action.extra.path.read_bytes(), b'fichier reapparu')
        self.assertEqual((manifest.parent / 'payload').read_bytes(), b'contenu identique')

    def test_corrupted_quarantine_prevents_restore(self):
        action = self.seed()[0]
        manifest = quarantine(action, Gate())
        (manifest.parent / 'payload').write_bytes(b'corrompu')
        with self.assertRaises(OSError):
            restore(manifest, Gate())
        self.assertFalse(action.extra.path.exists())

    def test_quarantine_symlink_is_rejected(self):
        action = self.seed()[0]
        outside = self.root / 'ailleurs'
        outside.mkdir()
        (self.root / QUARANTINE).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            quarantine(action, Gate())
        self.assertTrue(action.extra.path.exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_cancelled_action_does_not_move(self):
        action = self.seed()[0]
        gate = Gate()
        gate.stopped.set()
        with self.assertRaises(TaskStopped):
            quarantine(action, gate)
        self.assertTrue(action.extra.path.exists())

    def test_malicious_manifest_cannot_escape_source(self):
        action = self.seed()[0]
        manifest = quarantine(action, Gate())
        record = json.loads(manifest.read_text())
        record['relative'] = '../hors-source.jpg'
        manifest.write_text(json.dumps(record))
        with self.assertRaises(ValueError):
            restore(manifest, Gate())
        self.assertTrue((manifest.parent / 'payload').exists())

    def test_workspace_and_quarantine_are_excluded(self):
        workspace = self.root / 'travail'
        workspace.mkdir()
        qroot = self.root / QUARANTINE
        qroot.mkdir()
        for folder in [workspace, qroot]:
            (folder / 'photo.jpg').write_bytes(b'photo')
        (self.root / 'photo.jpg').write_bytes(b'photo')
        self.assertEqual(audit([self.root], Gate(), exclude=[workspace]), [])
        found = list(walk_photos(self.root, workspace, lambda error:None))
        self.assertFalse(any(QUARANTINE in path.parts for path in found))


if __name__ == '__main__':
    unittest.main()
