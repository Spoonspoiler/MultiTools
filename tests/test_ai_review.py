import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from PIL import Image

from multitools.photoarchiviste.ai_review import ReviewStore, analyse_index, context_key, validate_result
from multitools.photoarchiviste.inventory import Gate, Photo


RESULT={'scene':'paysage','qualite':'correcte','decision':'garder','confiance':'moyenne',
        'tags':['nature'],'observation':'Une image de test.'}


class FakeClient:
    def __init__(self): self.calls=[]
    def chat(self,model,messages,on_text,cancel,**kwargs):
        self.calls.append((model,messages,kwargs))
        return SimpleNamespace(text=json.dumps(RESULT))


class ReviewTests(unittest.TestCase):
    def test_personal_tags_work_before_any_ai_analysis_and_survive_reopening(self):
        photo=self.photo('manual.png','red')
        context=context_key('qwen-test','')
        with ReviewStore(self.db) as store:
            store.answer_manual(photo,'garder','famille',['Lucy','souvenir'],'Tag personnel')
            self.assertIsNone(store.cached(context,photo.sha256))
        with ReviewStore(self.db) as store:
            self.assertEqual(store.question(context,photo)['answer']['tags'],['Lucy','souvenir'])
            self.assertIn('Lucy',store.vocabulary())
        client=FakeClient()
        analyse_index([photo],self.db,client,'qwen-test','',Gate())
        self.assertEqual(len(client.calls),1)
        with ReviewStore(self.db) as store:
            question=store.question(context,photo)
            self.assertEqual(question['proposal']['tags'],['nature'])
            self.assertEqual(question['answer']['tags'],['Lucy','souvenir'])

    def test_manual_tag_routes_without_qwen_and_is_not_shared_with_other_copies(self):
        from multitools.photoarchiviste.tag_routing import plan_routes
        first=self.photo('manual-a.png','red')
        other=self.photo('manual-b.png','red')
        destination=self.root/'Famille'; destination.mkdir()
        context=context_key('qwen-test','')
        with ReviewStore(self.db) as store:
            store.set_route('Lucy',destination)
            store.answer_manual(first,'garder','famille',['Lucy'],'')
            self.assertEqual(plan_routes(store,context,[first])[0].destination,destination)
            self.assertIsNone(store.question(context,other))
    def test_cached_question_does_not_follow_a_changed_source(self):
        photo=self.photo('cache.png','red')
        client=FakeClient()
        result=analyse_index([photo],self.db,client,'qwen-test','',Gate())
        photo.path.write_bytes(b'autre fichier')
        with ReviewStore(self.db) as store:
            self.assertIsNone(store.question(result['context'],photo))
        again=analyse_index([photo],self.db,client,'qwen-test','',Gate())
        self.assertEqual(again['cached'],0)
        self.assertEqual(len(client.calls),1)
        self.assertTrue(again['errors'])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='multitools-ai-test-',dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.db=self.root/'review.sqlite3'

    def photo(self,name,color):
        path=self.root/name
        Image.new('RGB',(20,30),color).save(path)
        stat=path.stat()
        return Photo(path,stat.st_size,stat.st_mtime_ns,hashlib.sha256(path.read_bytes()).hexdigest(),20,30,format='PNG')

    def test_all_index_unique_contents_cache_and_no_source_mutation(self):
        photos=[self.photo('a.png','red'),self.photo('b.png','red'),self.photo('c.png','green')]
        before={p.path:p.path.read_bytes() for p in photos}
        client=FakeClient()
        result=analyse_index(photos,self.db,client,'qwen-test','garder souvenirs',Gate())
        self.assertEqual(result['processed'],2)
        self.assertEqual(len(client.calls),2)
        for photo in photos: self.assertEqual(photo.path.read_bytes(),before[photo.path])
        again=analyse_index(photos,self.db,client,'qwen-test','garder souvenirs',Gate())
        self.assertEqual(again['cached'],2)
        self.assertEqual(len(client.calls),2)
        with ReviewStore(self.db) as store:
            self.assertTrue(all(store.question(result['context'],p)['answer'] is None for p in photos))

    def test_cancellation_preserves_completed_analysis_and_resumes(self):
        photos=[self.photo('a.png','red'),self.photo('b.png','green')]
        gate=Gate()
        client=FakeClient()
        def progress(_): gate.stopped.set()
        result=analyse_index(photos,self.db,client,'qwen-test','',gate,progress)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['processed'],1)
        resumed=analyse_index(photos,self.db,client,'qwen-test','',Gate())
        self.assertEqual((resumed['processed'],resumed['cached']),(1,1))

    def test_changed_preferences_require_new_analysis(self):
        photo=self.photo('a.png','red')
        client=FakeClient()
        first=analyse_index([photo],self.db,client,'qwen-test','regle 1',Gate())
        second=analyse_index([photo],self.db,client,'qwen-test','regle 2',Gate())
        self.assertNotEqual(first['context'],second['context'])
        self.assertEqual(len(client.calls),2)

    def test_human_tags_corrections_and_export_are_persistent(self):
        photo=self.photo('a.png','red')
        client=FakeClient()
        result=analyse_index([photo],self.db,client,'qwen-test','',Gate())
        context=result['context']
        with ReviewStore(self.db) as store:
            self.assertEqual(store.vocabulary(),[])
            store.answer(context,photo,'garder','famille',['Lucy','souvenir'],'C’est un souvenir de famille')
        with ReviewStore(self.db) as store:
            question=store.question(context,photo)
            self.assertEqual(question['answer']['scene'],'famille')
            self.assertIn('Lucy',store.vocabulary())
            target=self.root/'annotations.jsonl'
            self.assertEqual(store.export(target),1)
            exported=json.loads(target.read_text())
            self.assertEqual(exported['tags'],['Lucy','souvenir'])
            self.assertEqual(exported['model_proposal']['scene'],'paysage')
            with self.assertRaises(FileExistsError): store.export(target)
        other=self.photo('b.png','green')
        analyse_index([other],self.db,client,'qwen-test','',Gate())
        prompt=client.calls[-1][1][-1]['content']
        self.assertIn('Lucy',prompt)
        self.assertIn('souvenir de famille',prompt)

    def test_failures_stop_after_three_and_do_not_create_questions(self):
        photos=[self.photo(str(i)+'.png',(i*20,0,0)) for i in range(5)]
        class Offline:
            calls=0
            def chat(inner,*args,**kwargs):
                inner.calls+=1
                raise OSError('Ollama indisponible')
        client=Offline()
        result=analyse_index(photos,self.db,client,'qwen-test','',Gate())
        self.assertEqual(client.calls,3)
        self.assertEqual(result['processed'],0)
        with ReviewStore(self.db) as store:
            self.assertIsNone(store.question(result['context'],photos[0]))

    def test_stale_index_is_not_analysed(self):
        photo=self.photo('a.png','red')
        photo.path.write_bytes(b'contenu modifie')
        client=FakeClient()
        result=analyse_index([photo],self.db,client,'qwen-test','',Gate())
        self.assertEqual(client.calls,[])
        self.assertTrue(result['errors'])

    def test_invalid_choices_and_unanalysed_annotations_are_rejected(self):
        with self.assertRaises(ValueError): validate_result({**RESULT,'decision':'delete'})
        with self.assertRaises(ValueError): validate_result({**RESULT,'tags':'famille'})
        photo=self.photo('a.png','red')
        with ReviewStore(self.db) as store:
            with self.assertRaises(ValueError):
                store.answer(context_key('qwen-test',''),photo,'garder','famille',[],'')
