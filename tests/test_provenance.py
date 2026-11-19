from base64 import b64decode
from io import BytesIO
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from PIL import Image
from multitools.provenance import link, resolve, analysis_view, sidecar
from multitools.operations import convert_file, IMAGES
from multitools.ollama import _image
from multitools.photoarchiviste.ai_review import analyse_index
from multitools.photoarchiviste.inventory import Photo, Gate

RESULT={'scene':'document','qualite':'correcte','decision':'garder','confiance':'moyenne',
        'tags':['carte'],'observation':'Carte en contours.'}


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(dir=Path.home()); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.source=self.root/'original.png'; Image.new('RGB',(30,40),'white').save(self.source)
        self.derived=self.root/'contours.png'; Image.new('RGBA',(30,40),(0,0,0,0)).save(self.derived)

    def photo(self):
        stat=self.derived.stat()
        return Photo(self.derived,stat.st_size,stat.st_mtime_ns,hashlib.sha256(self.derived.read_bytes()).hexdigest())

    def test_conversion_records_identity_and_keeps_original(self):
        before=self.source.read_bytes()
        op=next(op for op in IMAGES if op.extension=='.jpg')
        output=convert_file(self.source,self.root,op,Event())
        chain=resolve(output)
        self.assertTrue(chain[0]['source_available']); self.assertEqual(chain[0]['operation'],'JPEG')
        self.assertEqual(before,self.source.read_bytes())

    def test_manual_chain_and_changed_source(self):
        link(self.source,self.derived,'contours',manual=True)
        chain=resolve(self.derived); self.assertTrue(chain[0]['source_available'])
        with analysis_view(self.derived,chain) as path:
            self.assertTrue(path.exists())
            with Image.open(path) as image: self.assertEqual(image.size,(1200,600))
        self.assertFalse(path.exists())
        self.source.write_bytes(b'changed')
        self.assertFalse(resolve(self.derived)[0]['source_available'])
        self.derived.write_bytes(b'changed')
        with self.assertRaises(ValueError): resolve(self.derived)

    def test_transparency_sent_on_white(self):
        image=Image.open(BytesIO(b64decode(_image(self.derived))))
        self.assertGreater(image.getpixel((10,10))[0],245)

    def test_ai_reanalyses_after_link_and_receives_comparison(self):
        calls=[]
        class Client:
            def chat(inner,model,messages,on_text,cancel,**kwargs):
                with Image.open(kwargs['image']) as image: size=image.size
                calls.append((messages,size))
                return SimpleNamespace(text=json.dumps(RESULT))
        client=Client(); db=self.root/'review.sqlite3'; photo=self.photo()
        first=analyse_index([photo],db,client,'test','',Gate()); self.assertEqual(first['processed'],1,first)
        link(self.source,self.derived,'Contours transparents')
        again=analyse_index([photo],db,client,'test','',Gate())
        self.assertEqual(again['processed'],1,again)
        self.assertEqual(calls[-1][1],(1200,600))
        self.assertIn('même source documentaire',calls[-1][0][-1]['content'])
        cached=analyse_index([photo],db,client,'test','',Gate()); self.assertEqual(cached['cached'],1)
        self.assertEqual(len(calls),2)
