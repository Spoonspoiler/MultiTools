import unittest

from multitools.photoarchiviste.tag_editor import TagSelection


class TagSelectionTests(unittest.TestCase):
    def test_rejected_suggestion_does_not_return_when_reopening(self):
        selection=TagSelection(['nature','nuit'],['nature','nuit'])
        selection.toggle('nuit',False)
        selection.add(['Lucy','vacances'])
        restored=TagSelection(['nature','nuit'],selection.values())
        self.assertEqual(restored.values(),['nature','Lucy','vacances'])
        self.assertNotIn('nuit',restored.chosen)
        self.assertEqual(restored.personal(),['Lucy','vacances'])

    def test_case_whitespace_and_accent_variants_are_not_duplicates(self):
        selection=TagSelection(['famille'],[])
        selection.add([' Famille ','FAMILLE','été','e\u0301té'])
        self.assertEqual(len(selection.values()),2)
        self.assertEqual(selection.personal(),['été'])
        self.assertIn('famille',selection.chosen)

    def test_removing_personal_tag_keeps_other_choices(self):
        selection=TagSelection(['nature'],['nature','souvenir','Lucy'])
        selection.toggle('souvenir',False)
        self.assertEqual(selection.values(),['nature','Lucy'])

    def test_limit_does_not_partially_add_tags(self):
        selection=TagSelection([],['tag'+str(i) for i in range(19)])
        original=selection.values()
        with self.assertRaises(ValueError): selection.add(['un','deux'])
        self.assertEqual(selection.values(),original)

    def test_suggestion_with_a_comma_remains_one_tag(self):
        selection=TagSelection(['Paris, France'],['Paris, France'])
        self.assertEqual(selection.values(),['Paris, France'])
