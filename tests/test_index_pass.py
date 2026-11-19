import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from PIL import Image

from multitools.photoarchiviste.ai_review import ReviewStore, context_key
from multitools.photoarchiviste.index_pass import index_with_assistant
from multitools.photoarchiviste.inventory import Gate, TaskStopped
from multitools.photoarchiviste.journal import Archive


class IndexPassTests(unittest.TestCase):
    def test_index_button_handler_starts_the_combined_pass(self):
        from multitools.photoarchiviste.ui import PhotoArchiviste
        starts=[]
        with Archive(self.workspace) as archive:
            client=self.client(archive)
            assistant=SimpleNamespace(begin_pass=lambda:starts.append('assistant'))
            dedup=SimpleNamespace(model=SimpleNamespace(get=lambda:'qwen-test'),
                preferences=SimpleNamespace(get=lambda:''),client=client,index_assistant=assistant,
                work_tabs=SimpleNamespace(select=lambda value:None))
            def launch(title,work):
                self.assertIn('assistant',title)
                work(archive,Gate(),lambda *args:None)
            pane=SimpleNamespace(busy=False,workspace=self.workspace,sources=[self.source],dedup=dedup,
                preflight=None,tabs=SimpleNamespace(select=lambda value:None),_launch=launch)
            PhotoArchiviste._scan(pane)
            self.assertEqual(starts,['assistant'])
            self.assertEqual(client.calls,4)
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='multitools-pass-',dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'source'; self.source.mkdir()
        self.workspace=self.root/'travail'; self.workspace.mkdir()
        for name,color in [('a.png','red'),('b.png','green'),('c.png','blue'),('d.png','yellow')]:
            Image.new('RGB',(30,20),color).save(self.source/name)

    def client(self,archive):
        test=self
        class Client:
            calls=0
            def chat(inner,model,messages,on_text,cancel,**kwargs):
                inner.calls+=1
                # L'inventaire de la photo est déjà durable avant l'appel IA.
                test.assertTrue(any(photo.path==kwargs['image'] for photo in archive.photos()))
                return SimpleNamespace(text=json.dumps({'scene':'paysage','qualite':'correcte','decision':'garder',
                    'confiance':'moyenne','tags':['nature'],'observation':'Test du passage.'}))
        return Client()

    def test_first_index_calls_assistant_without_second_action(self):
        with Archive(self.workspace) as archive:
            client=self.client(archive)
            result=index_with_assistant(archive,[self.source],client,'qwen-test','',Gate(),lambda *args:None)
            self.assertEqual((result['indexed'],result['analysed'],client.calls),(4,4,4))
            photos=archive.photos()
        with ReviewStore(self.workspace/'assistant-tri.sqlite3') as store:
            self.assertTrue(all(store.question(context_key('qwen-test',''),photo) for photo in photos))
        self.assertEqual(len(list(self.source.iterdir())),4)

    def test_stop_and_resume_preserve_inventory_and_assistant_results(self):
        gate=Gate()
        def report(kind,value):
            if kind=='assistant_progress': gate.stopped.set()
        with Archive(self.workspace) as archive:
            client=self.client(archive)
            with self.assertRaises(TaskStopped):
                index_with_assistant(archive,[self.source],client,'qwen-test','',gate,report)
            self.assertTrue(archive.scan_incomplete)
            self.assertEqual(len(archive.photos()),1)
            resumed=index_with_assistant(archive,[self.source],client,'qwen-test','',Gate(),lambda *args:None)
            self.assertEqual(resumed['indexed'],4)
            self.assertEqual(resumed['analysed'],3)
            self.assertEqual(resumed['reused'],1)
            self.assertEqual(client.calls,4)
            self.assertFalse(archive.scan_incomplete)

    def test_offline_assistant_does_not_prevent_local_inventory(self):
        class Offline:
            calls=0
            def chat(inner,*args,**kwargs):
                inner.calls+=1
                raise OSError('Ollama hors ligne')
        client=Offline()
        with Archive(self.workspace) as archive:
            result=index_with_assistant(archive,[self.source],client,'qwen-test','',Gate(),lambda *args:None)
            self.assertEqual(result['indexed'],4)
            self.assertTrue(result['ai_paused'])
            self.assertEqual(client.calls,3)
            self.assertFalse(archive.scan_incomplete)
