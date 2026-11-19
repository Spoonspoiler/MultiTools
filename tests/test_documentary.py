import hashlib
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from multitools.documentary.model import document, geometry, Editing, Store
from multitools.documentary.imaging import preview, operation


class DocumentaryTests(unittest.TestCase):
    def test_roundtrip_and_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'carte.png'; Image.new('RGB',(120,90),'white').save(path)
            before=path.read_bytes(); data=document(path); editor=Editing(data)
            obj=geometry('polygon',[(0,0),(50,0),(50,40)])
            editor.change('Parcelle',lambda d:d['objects'].append(obj))
            editor.change('Numéro',lambda d:d['objects'][0].update(number='123'))
            editor.correct(obj['id'],None,obj,'cadastre',True)
            data['metadata']['Commune'].update(value='Test',status='corrige')
            store=Store(Path(folder)/'documents.sqlite3'); store.save(data)
            restored=store.load(data['id'])
            self.assertEqual(restored,data); self.assertEqual(restored['objects'][0]['number'],'123')
            self.assertEqual(len(store.examples('cadastre')),1)
            self.assertEqual(before,path.read_bytes())

    def test_undo_geometry_and_deletion(self):
        editor=Editing({'objects':[],'history':[]})
        obj=geometry('polygon',[(0,0),(5,0),(5,5)])
        editor.change('Créer',lambda d:d['objects'].append(obj))
        editor.change('Déplacer sommet',lambda d:d['objects'][0]['points'].__setitem__(0,[2,3]))
        editor.undo(); self.assertEqual(editor.data['objects'][0]['points'][0],[0,0])
        editor.redo(); self.assertEqual(editor.data['objects'][0]['points'][0],[2,3])
        editor.change('Supprimer',lambda d:d['objects'].clear()); editor.undo()
        self.assertEqual(len(editor.data['objects']),1)

    def test_tools_and_bounds(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'map.png'; Image.new('RGB',(3000,2000),'red').save(path)
            image,size=preview(path,600)
            self.assertEqual(size,(3000,2000)); self.assertEqual(image.size,(600,400))
            mask=operation(image,{'tool':'rouge'})
            self.assertEqual(mask.getpixel((10,10)),(255,255,255))
            self.assertEqual(operation(mask,{'tool':'contours'}).size,mask.size)
        for command in ({'tool':'exec','value':1},{'tool':'seuil','value':300},{'tool':'gris','code':'x'}):
            with self.assertRaises(ValueError): operation(Image.new('RGB',(4,4)),command)
        with self.assertRaises(ValueError): geometry('polygon',[(0,0),(1,1)])

    def test_overlay_and_analysis_contract(self):
        from multitools.documentary.imaging import overlay
        from multitools.documentary.model import validate_analysis
        base=Image.new('RGB',(10,10),'black'); other=Image.new('RGB',(10,10),'white')
        self.assertEqual(overlay(base,other,opacity=0).getpixel((5,5)),(0,0,0))
        self.assertEqual(overlay(base,other,opacity=1).getpixel((5,5)),(255,255,255))
        valid={'Commune':{'value':'','status':'hypothese','justification':'Illisible'}}
        self.assertEqual(validate_analysis(valid),valid)
        for invalid in ([], {'inconnu':{}}, {'Commune':{'value':'X','status':'valide'}}):
            with self.assertRaises(ValueError): validate_analysis(invalid)

    def test_ignore_is_not_validated_example(self):
        obj=geometry('polygon',[(0,0),(5,0),(5,5)],'ignorer')
        self.assertEqual(obj['exclusion'],'ignore'); self.assertNotEqual(obj['status'],'valide')


if __name__=='__main__': unittest.main()
