from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from multitools.folders import create_named_folder
from multitools import disk_picker


class FolderTests(unittest.TestCase):
    def test_named_folder_accepts_spaces_and_accents(self):
        with tempfile.TemporaryDirectory() as directory:
            created=create_named_folder(directory,'  Vacances été 2026  ')
            self.assertEqual(created.name,'Vacances été 2026')
            self.assertTrue(created.is_dir())
            self.assertEqual(list(created.iterdir()),[])

    def test_existing_folder_and_file_are_never_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory)
            folder=parent/'existant'; folder.mkdir()
            sentinel=folder/'archive.txt'; sentinel.write_text('conserver')
            document=parent/'document'; document.write_text('original')
            for name in ['existant','document']:
                with self.assertRaises(FileExistsError): create_named_folder(parent,name)
            self.assertEqual(sentinel.read_text(),'conserver')
            self.assertEqual(document.read_text(),'original')

    def test_a_name_cannot_escape_the_selected_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ['', '  ', '.', '..', '../ailleurs', '/tmp/ailleurs', 'sous/dossier', 'sous\\dossier', 'nom\x00']:
                with self.subTest(name=name),self.assertRaises(ValueError): create_named_folder(directory,name)
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_missing_parent_is_not_created_implicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            parent=Path(directory)/'absent'
            with self.assertRaises(FileNotFoundError): create_named_folder(parent,'test')
            self.assertFalse(parent.exists())

    def test_cancel_name_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(disk_picker,'askdirectory',return_value=directory), \
                 patch.object(disk_picker.simpledialog,'askstring',return_value=None):
                self.assertEqual(disk_picker.ask_new_directory(parent=None),'')
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_new_folder_is_returned_for_immediate_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(disk_picker,'askdirectory',return_value=directory), \
                 patch.object(disk_picker.simpledialog,'askstring',return_value='Photos famille'):
                result=disk_picker.ask_new_directory(parent=None)
            self.assertEqual(result,str(Path(directory)/'Photos famille'))
            self.assertTrue(Path(result).is_dir())
