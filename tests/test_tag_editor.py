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
        selection=TagSelection([],['tag'+str(i) for i in range(3)])
        original=selection.values()
        with self.assertRaises(ValueError): selection.add(['un','deux'])
        self.assertEqual(selection.values(),original)

    def test_four_tags_and_same_selection_on_another_photo(self):
        first=TagSelection([],['a','b','c','d'])
        other=TagSelection([],first.values())
        self.assertEqual(first.values(),other.values())
        with self.assertRaises(ValueError): first.add(['e'])
        self.assertEqual(len(first.values()),4)

    def test_persistence_validation_rejects_fifth_tag(self):
        from multitools.photoarchiviste.tags import validate
        from multitools.photoarchiviste.ai_review import validate_result, SCHEMA
        self.assertEqual(SCHEMA['properties']['tags']['maxItems'],4)
        self.assertEqual(validate(['A','a','b','c','d']),['A','b','c','d'])
        with self.assertRaises(ValueError): validate(['a','b','c','d','e'])
        result={'scene':'document','qualite':'correcte','decision':'garder','confiance':'moyenne',
                'tags':['a','b','c','d','e'],'observation':'Carte'}
        with self.assertRaises(ValueError): validate_result(result)

    def test_legacy_selection_can_be_reduced_without_losing_tags(self):
        selection=TagSelection([],['a','b','c','d','e'])
        self.assertEqual(len(selection.values()),5)
        selection.toggle('e',False)
        self.assertEqual(selection.values(),['a','b','c','d'])

    def test_suggestion_with_a_comma_remains_one_tag(self):
        selection=TagSelection(['Paris, France'],['Paris, France'])
        self.assertEqual(selection.values(),['Paris, France'])
