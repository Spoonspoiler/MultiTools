from pathlib import Path
import hashlib
import tempfile
import unittest

from multitools.photoarchiviste.ai_review import ReviewStore, context_key
from multitools.photoarchiviste.inventory import Photo
from multitools.photoarchiviste.tag_routing import plan_routes


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='multitools-route-',dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.family=self.root/'Famille'; self.family.mkdir()
        self.work=self.root/'Projets'; self.work.mkdir()
        self.rejects=self.root/'A_JETER'; self.rejects.mkdir()
        path=self.root/'source.jpg'; path.write_bytes(b'photo synthetique')
        info=path.stat()
        self.photo=Photo(path,info.st_size,info.st_mtime_ns,hashlib.sha256(path.read_bytes()).hexdigest())
        self.store=ReviewStore(self.root/'assistant.sqlite3')
        self.addCleanup(self.store.db.close)
        self.context=context_key('modele-test','')
        self.store.record(self.context,self.photo,'modele-test','',{'scene':'famille','qualite':'correcte',
            'decision':'garder','confiance':'forte','tags':['famille'],'observation':'Proposition non validée.'})

    def test_ai_tags_never_route_before_human_validation(self):
        self.store.set_route('famille',self.family)
        self.assertIsNone(plan_routes(self.store,self.context,[self.photo])[0].destination)
        self.store.answer(self.context,self.photo,'garder','famille',['Famille'],'')
        self.assertEqual(plan_routes(self.store,self.context,[self.photo])[0].destination,self.family)
        self.assertTrue(self.photo.path.exists())
        self.assertEqual(list(self.family.iterdir()),[])

    def test_conflicting_tags_require_a_destination_choice(self):
        self.store.set_route('famille',self.family)
        self.store.set_route('projet',self.work)
        self.store.answer(self.context,self.photo,'garder','famille',['famille','projet'],'')
        self.assertIsNone(plan_routes(self.store,self.context,[self.photo])[0].destination)
        self.store.answer(self.context,self.photo,'garder','famille',['famille','projet'],'',str(self.work))
        self.assertEqual(plan_routes(self.store,self.context,[self.photo])[0].destination,self.work)

    def test_rejects_use_only_the_single_reject_folder(self):
        self.store.set_route('famille',self.family)
        self.store.answer(self.context,self.photo,'a_jeter','famille',['famille'],'')
        self.assertEqual(plan_routes(self.store,self.context,[self.photo],self.rejects)[0].destination,self.rejects)

    def test_arbitrary_ai_destination_is_not_accepted(self):
        with self.assertRaises(ValueError):
            self.store.answer(self.context,self.photo,'garder','famille',['famille'],'',str(self.work))

    def test_reviewed_photo_and_unmapped_tags_stay_in_source(self):
        self.store.answer(self.context,self.photo,'a_revoir','famille',['famille'],'')
        self.assertIsNone(plan_routes(self.store,self.context,[self.photo])[0].destination)
        self.store.answer(self.context,self.photo,'garder','famille',['famille'],'')
        self.assertIsNone(plan_routes(self.store,self.context,[self.photo])[0].destination)

    def test_routes_survive_reopening(self):
        self.store.set_route('famille',self.family)
        with ReviewStore(self.root/'assistant.sqlite3') as reopened:
            self.assertEqual(reopened.destinations(['FAMILLE']),[str(self.family)])

    def test_symlink_route_is_refused(self):
        link=self.root/'lien'
        link.symlink_to(self.family,target_is_directory=True)
        with self.assertRaises(OSError): self.store.set_route('famille',link)

    def test_changed_rule_invalidates_old_explicit_destination(self):
        self.store.set_route('famille',self.family)
        self.store.set_route('projet',self.work)
        self.store.answer(self.context,self.photo,'garder','famille',['famille','projet'],'',str(self.work))
        self.store.set_route('projet',self.rejects)
        self.assertIsNone(plan_routes(self.store,self.context,[self.photo])[0].destination)

    def test_training_export_tracks_current_file_location(self):
        self.store.answer(self.context,self.photo,'garder','famille',['famille'],'')
        actual=self.family/'nouveau-nom.jpg'
        self.store.record_location(self.photo,actual)
        target=self.root/'export.jsonl'
        self.store.export(target)
        import json
        self.assertEqual(json.loads(target.read_text())['current_path'],str(actual))
