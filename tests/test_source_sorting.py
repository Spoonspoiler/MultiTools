import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from PIL import Image

from multitools.photoarchiviste.exact_duplicates import fingerprint
from multitools.photoarchiviste.inventory import Gate, Photo, walk_photos
from multitools.photoarchiviste.source_sorting import extract, restore_extraction, suggest, save_review, read_review, JOURNAL


class SortingTests(unittest.TestCase):
    def test_review_is_persistent_and_keeps_proposal_separate_from_decision(self):
        photo=Photo(Path('/source/test.jpg'),1,1,'abc')
        row={'photo':photo,'decision':'a_revoir','proposal':'a_jeter','reason':'Proposition IA','manifest':None}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'tri.json'
            save_review(path,[row],Path('/source/A_JETER'))
            saved,destination=read_review(path)
            self.assertEqual(saved[('/source/test.jpg','abc')]['decision'],'a_revoir')
            self.assertEqual(saved[('/source/test.jpg','abc')]['proposal'],'a_jeter')
            self.assertEqual(destination,'/source/A_JETER')

    def test_loading_duplicates_does_not_validate_rejections(self):
        from multitools.photoarchiviste.sort_ui import SourceSortPane
        root=Path('/source')
        photos=[Photo(root/'a.jpg',1,1,'same'),Photo(root/'b.jpg',1,1,'same')]
        pane=SimpleNamespace(idle=lambda:True,get_photos=lambda:photos,sources=lambda:[root],
                             destination=None,workspace=lambda:None,render=lambda:None)
        SourceSortPane.load(pane)
        self.assertEqual([row['decision'] for row in pane.rows],['a_revoir','a_revoir'])
        self.assertEqual([row['proposal'] for row in pane.rows],['garder','a_jeter'])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='multitools-tri-test-',dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'source'
        self.source.mkdir()
        self.destination=self.root/'A_JETER'
        self.destination.mkdir()

    def item(self,name='photo.jpg',content=b'photo temporaire'):
        path=self.source/name
        path.write_bytes(content)
        return fingerprint(path,self.source,Gate())

    def test_extracts_to_one_flat_folder_and_restores_without_copying_keeper(self):
        one=self.item()
        two=self.item('autre.jpg',b'autre photo')
        keeper=self.item('garder.jpg',b'souvenir')
        journals=[extract(item,self.destination,Gate()) for item in [one,two]]
        self.assertEqual(keeper.path.read_bytes(),b'souvenir')
        self.assertFalse(one.path.exists())
        self.assertFalse(two.path.exists())
        self.assertEqual([p.name for p in self.destination.iterdir() if p.is_dir()],[JOURNAL])
        for journal,item in zip(journals,[one,two]):
            target=restore_extraction(journal,Gate())
            self.assertEqual(target,item.path)
            self.assertEqual(target.read_bytes(),b'photo temporaire' if item==one else b'autre photo')

    def test_name_collisions_never_overwrite(self):
        existing=self.destination/'photo.jpg'
        existing.write_bytes(b'existant')
        item=self.item()
        journal=extract(item,self.destination,Gate())
        self.assertEqual(existing.read_bytes(),b'existant')
        item.path.write_bytes(b'reapparu')
        with self.assertRaises(OSError): restore_extraction(journal,Gate())
        self.assertEqual(item.path.read_bytes(),b'reapparu')
        self.assertTrue((self.destination/json.loads(journal.read_text())['target']).exists())

    def test_changed_source_and_symlink_destination_block_extraction(self):
        item=self.item()
        item.path.write_bytes(b'changement')
        with self.assertRaises(OSError): extract(item,self.destination,Gate())
        item=fingerprint(item.path,self.source,Gate())
        link=self.root/'raccourci'
        link.symlink_to(self.destination,target_is_directory=True)
        with self.assertRaises(OSError): extract(item,link,Gate())
        self.assertTrue(item.path.exists())

    def test_destination_is_not_reindexed_when_inside_source(self):
        destination=self.source/'rejets'
        destination.mkdir()
        item=self.item()
        extract(item,destination,Gate())
        keeper=self.item('garder.jpg')
        self.assertEqual(list(walk_photos(self.source,self.root/'workspace',lambda error:None)),[keeper.path])

    def test_ai_proposals_are_read_only_and_missing_answers_mean_review(self):
        path=self.source/'test.png'
        Image.new('RGB',(50,80),'green').save(path)
        photo=Photo(path,path.stat().st_size,path.stat().st_mtime_ns,'example',50,80,format='PNG')
        original=path.read_bytes()
        class Client:
            def chat(inner,model,messages,on_text,cancel,**options):
                self.assertTrue(options['image'].is_file())
                self.assertEqual(len(messages),2)
                return SimpleNamespace(text=json.dumps({'photos':[{'id':1,'decision':'a_jeter','raison':'Proposition test'}]}))
        self.assertEqual(suggest(Client(),'modele-test',[photo],'priorites',Gate()),[('a_jeter','Proposition test')])
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(list(self.destination.iterdir()),[])
        class Missing:
            def chat(inner,*args,**kwargs): return SimpleNamespace(text='{"photos":[]}')
        self.assertEqual(suggest(Missing(),'modele-test',[photo],'',Gate())[0][0],'a_revoir')

    def test_invalid_ai_ids_are_rejected(self):
        path=self.source/'test.png'
        Image.new('RGB',(10,10)).save(path)
        photo=Photo(path,path.stat().st_size,1,'example')
        class Invalid:
            def chat(inner,*args,**kwargs):
                return SimpleNamespace(text='{"photos":[{"id":99,"decision":"a_jeter","raison":""}]}')
        with self.assertRaises(ValueError): suggest(Invalid(),'modele-test',[photo],'',Gate())
