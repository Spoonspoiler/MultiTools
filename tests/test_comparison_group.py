from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from multitools.photoarchiviste.comparison_group import comparison_group,partition_group,highest_resolution
from multitools.photoarchiviste.inventory import Photo
from multitools.photoarchiviste.preview import _detail_crop


class GroupTests(unittest.TestCase):
    def rows(self):
        small=Photo(Path('/source/petite.jpg'),500,1,'a',800,600,format='JPEG')
        large=Photo(Path('/source/grande.jpg'),100,1,'b',4000,3000,format='JPEG')
        same=replace(small,path=Path('/autre/copie.jpg'))
        different=Photo(Path('/source/autre.jpg'),900,1,'c',1000,1000,format='JPEG')
        return [{'photo':photo,'decision':'a_revoir','manifest':None} for photo in (small,large,same,different)]

    def test_group_includes_all_exact_copies_and_visual_variants(self):
        rows=self.rows()
        current=rows[0]['photo']
        group=comparison_group(rows,current,(rows[1]['photo'],))
        self.assertEqual({p.path for p in group},{rows[i]['photo'].path for i in (0,1,2)})
        self.assertEqual(group[0].path,rows[1]['photo'].path)

    def test_maximum_resolution_does_not_mean_largest_file(self):
        photos=[row['photo'] for row in self.rows()]
        self.assertEqual(highest_resolution(photos),{Path('/source/grande.jpg')})
        self.assertEqual(max(photos,key=lambda p:p.size).path,Path('/source/autre.jpg'))

    def test_selection_preserves_all_chosen_variants_and_only_rejects_rest(self):
        rows=self.rows()
        group=comparison_group(rows,rows[0]['photo'],(rows[1]['photo'],))
        keep,reject=partition_group(rows,group,{rows[1]['photo'].path})
        self.assertEqual([r['photo'].path for r in keep],[Path('/source/grande.jpg')])
        self.assertEqual({r['photo'].path for r in reject},{Path('/source/petite.jpg'),Path('/autre/copie.jpg')})
        self.assertTrue(all(row['decision']=='a_revoir' for row in rows))

    def test_empty_selection_requires_explicit_reject_all(self):
        rows=self.rows()
        group=comparison_group(rows,rows[0]['photo'])
        with self.assertRaises(ValueError): partition_group(rows,group,set())
        keep,reject=partition_group(rows,group,set(),allow_empty=True)
        self.assertEqual(keep,[])
        self.assertEqual(len(reject),2)

    def test_outsider_and_stale_group_are_rejected(self):
        rows=self.rows()
        group=comparison_group(rows,rows[0]['photo'])
        with self.assertRaises(ValueError): partition_group(rows,group,{Path('/hors-source.jpg')})
        rows[0]['photo']=replace(rows[0]['photo'],sha256='modifie')
        with self.assertRaises(ValueError): partition_group(rows,group,{group[0].path})

    def test_native_detail_crop_preserves_pixels_instead_of_resizing(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'details.png'
            image=Image.new('RGB',(80,60))
            for x in range(80):
                for y in range(60): image.putpixel((x,y),(x,y,0))
            image.save(path)
            original=path.read_bytes()
            crop=_detail_crop(path,(20,10))
            self.assertEqual(crop.size,(20,10))
            self.assertEqual(crop.getpixel((10,5))[:3],(40,30,0))
            corner=_detail_crop(path,(20,10),(0,0))
            self.assertEqual(corner.getpixel((0,0))[:3],(0,0,0))
            self.assertEqual(path.read_bytes(),original)
