from pathlib import Path
import unittest

from multitools.photoarchiviste.inventory import Comparison, Photo
from multitools.photoarchiviste.review import filter_cases, photo_format


class PhotoFilterTests(unittest.TestCase):
    def test_filter_keeps_other_format_in_comparison_group(self):
        jpeg = Photo(Path('/disque-a/photo.jpg'), 20, 1, 'a', format='JPEG')
        png = Photo(Path('/disque-b/photo.png'), 40, 1, 'b', format='PNG')
        group = Comparison((jpeg, png), 'Proches visuellement')
        self.assertEqual(filter_cases([group], 'PNG'), [group])
        self.assertEqual(filter_cases([group], 'PNG')[0].photos, (jpeg, png))
        self.assertEqual(filter_cases([group], 'WEBP'), [])
        self.assertEqual(group.photos, (jpeg, png))

    def test_all_photos_view_filters_single_files_and_restores_all(self):
        cases = [Comparison((Photo(Path('a.jpg'), 1, 1, 'a'),), ''),
                 Comparison((Photo(Path('b.png'), 1, 1, 'b'),), '')]
        self.assertEqual(filter_cases(cases, 'JPEG'), cases[:1])
        self.assertEqual(filter_cases(cases, 'Tous les formats'), cases)

    def test_detected_format_takes_precedence_over_extension(self):
        self.assertEqual(photo_format(Photo(Path('fake.png'), 1, 1, 'a', format='JPEG')), 'JPEG')
        self.assertEqual(photo_format(Photo(Path('photo.TIF'), 1, 1, 'b')), 'TIFF')


if __name__ == '__main__':
    unittest.main()
