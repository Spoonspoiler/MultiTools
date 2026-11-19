from pathlib import Path
import unittest

from multitools.photoarchiviste.inventory import Photo
from multitools.photoarchiviste.manual_review import manual_targets, with_manual_rejections
from multitools.photoarchiviste.tag_routing import Route


class ManualReviewTests(unittest.TestCase):
    def rows(self):
        return [{'photo':Photo(Path(name),10,1,sha),'decision':'a_revoir','manifest':None}
                for name,sha in [('/source/a.jpg','same'),('/autre/b.jpg','same'),('/source/proche.jpg','different')]]

    def test_can_reject_whole_exact_group_including_last_exemplar(self):
        rows=self.rows()
        targets=manual_targets(rows,rows[0]['photo'],True)
        self.assertEqual([row['photo'].path for row in targets],[Path('/source/a.jpg'),Path('/autre/b.jpg')])
        self.assertEqual(manual_targets(rows[:1],rows[0]['photo'],True),rows[:1])

    def test_single_photo_choice_does_not_include_other_variants(self):
        rows=self.rows()
        self.assertEqual(manual_targets(rows,rows[0]['photo']),rows[:1])

    def test_already_extracted_photos_are_not_scheduled_again(self):
        rows=self.rows()
        rows[1]['manifest']=Path('/A_JETER/journal.json')
        self.assertEqual(manual_targets(rows,rows[0]['photo'],True),rows[:1])

    def test_no_ai_result_is_required_for_manual_rejections(self):
        rows=self.rows()
        for row in rows[:2]: row['decision']='a_jeter'
        pending=[Route(row['photo'],None,'Tags non analysés') for row in rows]
        plan=with_manual_rejections(pending,rows,Path('/A_JETER'))
        self.assertEqual([route.destination for route in plan],[Path('/A_JETER'),Path('/A_JETER'),None])
        self.assertEqual(plan[0].status,'Rejet manuel validé')

    def test_reject_folder_must_be_chosen_and_order_is_checked(self):
        rows=self.rows()
        rows[0]['decision']='a_jeter'
        plan=[Route(row['photo'],None,'En attente') for row in rows]
        self.assertIsNone(with_manual_rejections(plan,rows,None)[0].destination)
        with self.assertRaises(ValueError): with_manual_rejections(plan[::-1],rows,Path('/A_JETER'))

    def test_stale_display_is_rejected(self):
        rows=self.rows()
        stale=Photo(rows[0]['photo'].path,10,1,'old')
        with self.assertRaises(ValueError): manual_targets(rows,stale,True)
