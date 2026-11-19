from __future__ import annotations

from errno import ENOSPC
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from multitools.photoarchiviste import journal, path_safety, transfers
from multitools.photoarchiviste.inventory import Gate, Photo, TaskStopped, comparisons
from multitools.photoarchiviste.journal import Archive
from multitools.photoarchiviste.review import Decision, propose, propose_selection
from multitools.photoarchiviste.transfers import execute_pending
from multitools.photoarchiviste.ui import PhotoArchiviste
from multitools.storage import Mount, guard_target


def quiet(*_args):
    pass


class InterruptedProcess(BaseException):
    pass


class PhotoArchivisteTests(unittest.TestCase):
    def setUp(self):
        sandbox = TemporaryDirectory(prefix="photo-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name).resolve()
        self.left, self.right, self.output = [self.root / name for name in ("Disque 1", "Disque 2", "Archive")]
        for directory in (self.left, self.right, self.output):
            directory.mkdir()

    @staticmethod
    def paint(path: Path) -> None:
        image = Image.new("RGB", (160, 125), "white")
        drawing = ImageDraw.Draw(image)
        drawing.ellipse((12, 24, 90, 117), fill="black")
        drawing.rectangle((104, 12, 150, 98), fill="steelblue")
        exif = Image.Exif()
        exif[36867] = "2019:04:15 07:30:00"
        image.save(path, exif=exif)

    def exact_pair(self, name="photo.png"):
        left, right = self.left / name, self.right / name
        self.paint(left)
        right.write_bytes(left.read_bytes())
        return left, right

    def scan(self, archive: Archive):
        archive.scan([self.left, self.right], Gate(), quiet)
        return archive.photos()

    def test_exact_group_tracks_three_variants_and_unique_photos(self):
        left, right = self.exact_pair()
        third = self.left / "copie.png"
        third.write_bytes(left.read_bytes())
        unique = self.right / "unique.png"
        Image.new("RGB", (40, 40), "red").save(unique)
        with Archive(self.output) as archive:
            photos = self.scan(archive)
            cases = comparisons(photos, archive.withheld(), Gate())
            group = cases["Exacts"][0]
            self.assertEqual({photo.path for photo in group.photos}, {left, right, third})
            self.assertEqual(len(cases["Toutes"]), 4)
            primary = next(photo for photo in group.photos if photo.path == left)
            secondary = next(photo for photo in group.photos if photo.path == right)
            pending = propose(archive, Decision.KEEP_LEFT, primary, secondary, "Famille", "À classer",
                              group.reason, move=False, by_year=True)
            self.assertFalse((self.output / "TRIEES").exists())
            self.assertFalse((self.output / "A_JETER").exists())
            self.assertEqual(pending[0].target.parent, self.output / "TRIEES" / "Famille" / "2019")
            after = comparisons(photos, archive.withheld(), Gate())
            self.assertEqual(after["Exacts"][0].photos[0].path, third)
            self.assertEqual({case.photos[0].path for case in after["Toutes"]}, {third, unique})

    def test_visual_close_keeps_distinct_digests(self):
        original = self.left / "original.png"
        self.paint(original)
        with Image.open(original) as image:
            image.resize((80, 63), Image.Resampling.LANCZOS).save(self.right / "mini.jpg", quality=68)
        with Archive(self.output) as archive:
            photos = self.scan(archive)
            result = comparisons(photos, set(), Gate())
            self.assertFalse(result["Exacts"])
            self.assertEqual(len(result["Proches"]), 1)
            self.assertNotEqual(photos[0].sha256, photos[1].sha256)
            self.assertTrue(all(photo.path.exists() for photo in photos))
            self.assertFalse(archive.transfers())

    def test_visual_series_merges_matching_shots_and_keeps_duplicate_copies(self):
        # The extremes differ by eight bits. Each is four bits from the middle shot.
        first, middle, last, duplicate = [self.left / name for name in ("1.jpg", "2.jpg", "3.jpg", "3-copie.jpg")]
        photos = [Photo(path, 12, 0, digest, 160, 125, visual=f"p{bits:016x}", color="120,120,120")
                  for path, digest, bits in ((first, "a", 0), (middle, "b", 0xF), (last, "c", 0xFF), (duplicate, "c", 0xFF))]
        cases = comparisons(photos, set(), Gate())
        self.assertEqual(len(cases["Proches"]), 1)
        self.assertEqual({photo.path for photo in cases["Proches"][0].photos}, {first, middle, last, duplicate})
        self.assertEqual({photo.path for photo in cases["Exacts"][0].photos}, {last, duplicate})
        remaining, = comparisons(photos, {middle}, Gate())["Proches"]
        self.assertEqual({photo.path for photo in remaining.photos}, {first, last, duplicate})

    def test_series_selection_only_plans_chosen_files(self):
        left, _ = self.exact_pair("Original.png")
        names = [self.left / f"Photo-{index}.png" for index in range(6)]
        for copy in names:
            copy.write_bytes(left.read_bytes())
        with Archive(self.output) as archive:
            photos = self.scan(archive)
            series, = comparisons(photos, archive.withheld(), Gate())["Proches"]
            self.assertEqual(len(series.photos), 8)
            chosen = (series.photos[0], series.photos[7])
            planned = propose_selection(archive, chosen, "Famille", series.reason, move=False, by_year=True, gate=Gate())
            self.assertEqual({item.source for item in planned}, {photo.path for photo in chosen})
            self.assertTrue(all(item.kind == "keep" and item.target.parent.name == "2019" for item in planned))
            self.assertFalse((self.output / "TRIEES").exists())
            self.assertFalse((self.output / "A_JETER").exists())
            remaining, = comparisons(photos, archive.withheld(), Gate())["Proches"]
            self.assertEqual(len(remaining.photos), 6)

    def test_keep_both_preserves_colliding_names(self):
        left, right = self.exact_pair()
        with Archive(self.output) as archive:
            photos = self.scan(archive)
            first, second = propose(archive, Decision.KEEP_BOTH, *photos, "Famille", "Famille", "SHA-256",
                                    move=False, by_year=False)
            self.assertNotEqual(first.target, second.target)
            first.target.parent.mkdir(parents=True)
            first.target.write_bytes(b"un fichier existant")
            self.assertEqual(execute_pending(archive, Gate(), quiet), (2, 0))
            self.assertEqual(first.target.read_bytes(), b"un fichier existant")
            for item in archive.transfers():
                self.assertEqual(item.target.read_bytes(), left.read_bytes())
            self.assertTrue(right.exists())

    def test_keep_right_and_different_categories(self):
        self.exact_pair()
        with Archive(self.output) as archive:
            photos = self.scan(archive)
            left, right = propose(archive, Decision.KEEP_RIGHT, *photos, "Voyages", "Projets/Photo", "raison",
                                  move=False, by_year=False)
            self.assertEqual(left.kind, "reject")
            self.assertEqual(right.target.parent, self.output / "TRIEES" / "Projets" / "Photo")
            self.assertEqual(execute_pending(archive, Gate(), quiet), (2, 0))
            self.assertEqual(right.target.read_bytes(), right.source.read_bytes())
            self.assertEqual(left.target.read_bytes(), left.source.read_bytes())

    def test_reject_both_and_restore_from_reopened_journal(self):
        left, right = self.exact_pair()
        original = left.read_bytes()
        with Archive(self.output) as archive:
            photos = self.scan(archive)
            items = propose(archive, Decision.REJECT_BOTH, *photos, "À classer", "À classer", "raison",
                            move=False, by_year=False)
            self.assertEqual(execute_pending(archive, Gate(), quiet), (2, 0))
            self.assertEqual(left.read_bytes(), original)
            self.assertEqual(right.read_bytes(), original)
            self.assertTrue(all(item.target.parent == self.output / "A_JETER" for item in items))
        left.write_bytes(b"emplacement reutilise")
        with Archive(self.output) as archive:
            with self.assertRaisesRegex(ValueError, "restauration par déplacement"):
                archive.plan_restore([item.id for item in items])
            self.assertEqual(archive.plan_restore([item.id for item in items], copy=True), 2)
            self.assertEqual(execute_pending(archive, Gate(), quiet), (2, 0))
            reverse = [item for item in archive.transfers() if item.kind == "restore"]
            self.assertTrue(all(item.mode == "copy" and item.target not in {left, right} for item in reverse))
            self.assertTrue(all(item.target.read_bytes() == original for item in reverse))
            self.assertEqual(left.read_bytes(), b"emplacement reutilise")
            self.assertEqual(right.read_bytes(), original)
            self.assertTrue(all(item.target.read_bytes() == original for item in items))

    def test_resume_after_rename_before_sqlite_commit(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        with Archive(self.output) as archive:
            photo = self.scan(archive)[0]
            item, = propose(archive, Decision.KEEP_LEFT, photo, None, "Documents", "", "raison",
                            move=False, by_year=False)
            publish = transfers._publish_no_replace
            def interrupted(*args, **kwargs):
                publish(*args, **kwargs)
                raise InterruptedProcess
            with patch.object(transfers, "_publish_no_replace", interrupted), self.assertRaises(InterruptedProcess):
                execute_pending(archive, Gate(), quiet)
            self.assertEqual(archive.transfer(item.id).state, "ready")
            self.assertEqual(left.read_bytes(), original)
            self.assertEqual(item.target.read_bytes(), original)
        with Archive(self.output) as archive:
            self.assertEqual(execute_pending(archive, Gate(), quiet), (1, 0))
            self.assertEqual(len(list(item.target.parent.glob("*.png"))), 1)
            self.assertEqual(left.read_bytes(), original)
            self.assertEqual(item.target.read_bytes(), original)

    def test_corrupted_output_prevents_success(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        with Archive(self.output) as archive:
            photo = self.scan(archive)[0]
            item, = propose(archive, Decision.KEEP_LEFT, photo, None, "Documents", "", "raison",
                            move=False, by_year=False)
            with patch.object(transfers, "_finish", side_effect=InterruptedProcess), self.assertRaises(InterruptedProcess):
                execute_pending(archive, Gate(), quiet)
            self.assertEqual(archive.transfer(item.id).state, "verified")
            item.target.write_bytes(b"autre fichier")
            self.assertEqual(execute_pending(archive, Gate(), quiet), (0, 1))
            self.assertEqual(left.read_bytes(), original)
            self.assertEqual(item.target.read_bytes(), b"autre fichier")
            self.assertEqual(archive.transfer(item.id).state, "verified")
            self.assertIn("diffère", archive.transfer(item.id).error)

    def test_interrupted_scan_reuses_only_completed_rows(self):
        self.exact_pair()
        with Archive(self.output) as archive:
            gate = Gate()
            visits = 0
            def interrupt(kind, _value):
                nonlocal visits
                if kind == "file":
                    visits += 1
                    if visits == 2:
                        gate.stopped.set()
            with self.assertRaises(TaskStopped):
                archive.scan([self.left, self.right], gate, interrupt)
            self.assertTrue(archive.scan_incomplete)
            self.assertEqual(len(archive.photos()), 1)
        with Archive(self.output) as archive:
            with patch.object(journal, "inspect_photo", wraps=journal.inspect_photo) as inspect:
                self.assertEqual(archive.scan(archive.roots, Gate(), quiet), (2, 0))
                self.assertEqual(inspect.call_count, 1)
            self.assertFalse(archive.scan_incomplete)

    def test_interrupted_scan_of_new_sources_does_not_show_old_inventory(self):
        left, _ = self.exact_pair()
        with Archive(self.output) as archive:
            archive.scan([self.left], Gate(), quiet)
            self.assertEqual([photo.path for photo in archive.photos()], [left])
            gate = Gate()
            gate.stopped.set()
            with self.assertRaises(TaskStopped):
                archive.scan([self.right], gate, quiet)
            self.assertEqual(archive.photos(), [])
            self.assertEqual(archive.roots, [self.right])

    def test_changed_source_is_never_replaced_or_moved(self):
        left, _ = self.exact_pair()
        replacement = b"contenu plus recent"
        with Archive(self.output) as archive:
            photo = self.scan(archive)[0]
            item, = propose(archive, Decision.KEEP_LEFT, photo, None, "Documents", "", "raison",
                            move=False, by_year=False)
            left.write_bytes(replacement)
            self.assertEqual(execute_pending(archive, Gate(), quiet), (0, 1))
            self.assertEqual(left.read_bytes(), replacement)
            self.assertFalse(item.target.exists())
            self.assertFalse(list(item.target.parent.glob("*.part")))

    def test_device_import_is_a_copy_and_rejects_move(self):
        phone = self.left / "gvfs"
        phone.mkdir()
        left = phone / "Téléphone.png"
        self.paint(left)
        with Archive(self.output) as archive:
            photo = self.scan(archive)[0]
            with self.assertRaisesRegex(ValueError, "Mode conservatoire"):
                propose(archive, Decision.KEEP_LEFT, photo, None, "Famille", "", "Source téléphone",
                        move=True, by_year=False)
            self.assertFalse(archive.transfers())
            item, = propose(archive, Decision.KEEP_LEFT, photo, None, "Famille", "", "Source téléphone",
                            move=False, by_year=False)
            self.assertEqual(item.mode, "copy")
            self.assertEqual(execute_pending(archive, Gate(), quiet), (1, 0))
            self.assertEqual(item.target.read_bytes(), left.read_bytes())

    def test_transfer_stays_pending_when_configured_disk_is_absent(self):
        left, _ = self.exact_pair()
        with Archive(self.output) as archive:
            photo = self.scan(archive)[0]
            item, = propose(archive, Decision.KEEP_LEFT, photo, None, "Famille", "", "Destination test",
                            move=False, by_year=False)
            def missing(_path, _count):
                raise OSError("Disque absent")
            self.assertEqual(execute_pending(archive, Gate(), quiet, missing), (0, 1))
            self.assertEqual(archive.transfer(item.id).state, "planned")
            self.assertEqual(left.read_bytes(), self.right.joinpath(left.name).read_bytes())
            self.assertFalse(item.target.parent.exists())

    def test_deferred_and_invalid_category_have_no_side_effect_on_sources(self):
        left, right = self.exact_pair()
        with Archive(self.output) as archive:
            photos = self.scan(archive)
            archive.defer((photos[0],))
            view = comparisons(archive.photos(), archive.withheld(), Gate())
            self.assertEqual(view["Reportées"][0].photos[0].path, left)
            with self.assertRaises(ValueError):
                propose(archive, Decision.KEEP_BOTH, *photos, "Famille", "../hors-de-la-destination", "raison",
                        move=False, by_year=False)
            self.assertFalse(archive.transfers())
            self.assertTrue(left.exists() and right.exists())

    def prepare_copy(self, archive):
        photo = self.scan(archive)[0]
        item, = propose(archive, Decision.KEEP_LEFT, photo, None, "Documents", "", "essai",
                        move=False, by_year=False)
        return item

    def test_source_substitution_after_final_digest(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        replacement = self.left / "replacement.tmp"
        replacement.write_bytes(b"nouveau contenu unique")
        check_digest = transfers._check_digest
        with Archive(self.output) as archive:
            item = self.prepare_copy(archive)

            def substitute(path, transfer, gate, **kwargs):
                proof = check_digest(path, transfer, gate, **kwargs)
                if transfer.state == "verified":
                    replacement.replace(left)
                return proof

            with patch.object(transfers, "_check_digest", substitute), patch.object(Path, "unlink") as unlink:
                self.assertEqual(execute_pending(archive, Gate(), quiet), (0, 1))
            unlink.assert_not_called()
            self.assertEqual(left.read_bytes(), b"nouveau contenu unique")
            self.assertEqual(item.target.read_bytes(), original)
            self.assertEqual(archive.transfer(item.id).state, "verified")

    def test_destination_disappears_after_final_digest(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        check_digest = transfers._check_digest
        remove_test_file = Path.unlink
        with Archive(self.output) as archive:
            item = self.prepare_copy(archive)

            def disappear(path, transfer, gate, **kwargs):
                proof = check_digest(path, transfer, gate, **kwargs)
                if transfer.state == "verified":
                    remove_test_file(path)
                return proof

            with patch.object(transfers, "_check_digest", disappear), patch.object(Path, "unlink") as unlink:
                self.assertEqual(execute_pending(archive, Gate(), quiet), (0, 1))
            unlink.assert_not_called()
            self.assertEqual(left.read_bytes(), original)
            self.assertFalse(item.target.exists())
            self.assertEqual(archive.transfer(item.id).state, "verified")

    def test_legacy_journal_blocks_moves_and_unproven_copies_at_every_phase(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        target_dir = self.output / "TRIEES"
        target_dir.mkdir()
        target = target_dir / "existant.png"
        target.write_bytes(original)
        temporary = target_dir / (".photoarchiviste-" + "a" * 32 + ".part")
        partial_content = "temporaire issu de l’ancienne version".encode()
        temporary.write_bytes(partial_content)
        stat = target.stat()
        with sqlite3.connect(self.output / "PhotoArchiviste.sqlite3") as old:
            old.executescript(journal.SCHEMA.split("CREATE TABLE IF NOT EXISTS photo_evidence", 1)[0])
            rows = [(mode, state, "keep") for mode in ("move", "copy") for state in journal.PENDING]
            rows.extend([("move", "verified", "restore"), ("move", "done", "keep")])
            old.executemany("""
                INSERT INTO transfers (source, target, sha256, size, decision, mode, state, kind, staging,
                                       staged_dev, staged_ino)
                VALUES (?, ?, ?, ?, 'ancien', ?, ?, ?, ?, ?, ?)
            """, [(str(left), str(target), hashlib.sha256(original).hexdigest(), len(original), *row,
                   temporary.name, str(stat.st_dev), str(stat.st_ino)) for row in rows])
        old.close()
        with Archive(self.output) as archive, patch.object(Path, "unlink") as unlink:
            self.assertEqual(execute_pending(archive, Gate(), quiet), (0, 9))
            unlink.assert_not_called()
            self.assertEqual([(t.mode, t.state, t.kind) for t in archive.transfers()], rows)
            self.assertTrue(all(not t.error for t in archive.transfers() if t.state == "done"))
        self.assertEqual(left.read_bytes(), original)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(temporary.read_bytes(), partial_content)

    def test_point_de_montage_absent_ou_remplace(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        with Archive(self.output) as archive:
            item = self.prepare_copy(archive)
            role = {"DATA": {"path": str(self.output), "uuid": "attendu"}}
            for observed in ([], [Mount(self.output, "autre", frozenset({"disque"}), "disque")]):
                with self.subTest(observed=observed), patch("multitools.storage.mounts", return_value=observed):
                    self.assertEqual(execute_pending(archive, Gate(), quiet,
                                                     lambda path, size: guard_target(path, size, role)), (0, 1))
                self.assertTrue(self.output.is_dir())
                self.assertFalse(item.target.parent.exists())
                self.assertEqual(left.read_bytes(), original)
                self.assertEqual(archive.transfer(item.id).state, "planned")

    def test_changed_native_volume_identity_blocks_even_without_configured_role(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        volume_at = path_safety.volume_at
        with Archive(self.output) as archive:
            item = self.prepare_copy(archive)

            def replaced_mount(path, info, mounts=None):
                volume = volume_at(path, info, mounts)
                if path == self.output:
                    return path_safety.Volume(volume.mount, "remplacement", True)
                return volume

            with patch.object(path_safety, "volume_at", replaced_mount):
                self.assertEqual(execute_pending(archive, Gate(), quiet), (0, 1))
            self.assertFalse(item.target.parent.exists())
            self.assertEqual(left.read_bytes(), original)

    def test_replaced_destination_parent_after_staging_blocks_publication(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        with Archive(self.output) as archive:
            item = self.prepare_copy(archive)
            with patch.object(transfers, "_publish", side_effect=InterruptedProcess), self.assertRaises(InterruptedProcess):
                execute_pending(archive, Gate(), quiet)
            ready = archive.transfer(item.id)
            relocated = ready.target.parent.rename(ready.target.parent.with_name("ancien"))
            ready.target.parent.mkdir()
            marker = ready.target.parent / ready.staging
            marker.write_bytes(b"fichier tiers")
            self.assertEqual(execute_pending(archive, Gate(), quiet), (0, 1))
            self.assertEqual(marker.read_bytes(), b"fichier tiers")
            self.assertEqual((relocated / ready.staging).read_bytes(), original)
            self.assertEqual(left.read_bytes(), original)
            self.assertFalse(item.target.exists())

    def test_replacement_with_same_bytes_and_mtime_after_indexation(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        with Archive(self.output) as archive:
            photo = self.scan(archive)[0]
            times = left.stat()
            other = self.left / "nouveau.tmp"
            other.write_bytes(original)
            os.utime(other, ns=(times.st_atime_ns, times.st_mtime_ns))
            other.replace(left)
            with self.assertRaisesRegex(OSError, "Relance l’indexation"):
                propose(archive, Decision.KEEP_LEFT, photo, None, "Documents", "", "", move=False, by_year=False)
            self.assertFalse(archive.transfers())
            self.assertEqual(left.read_bytes(), original)
            self.assertFalse((self.output / "TRIEES").exists())

    def test_cancel_mid_copy_and_resume_without_reusing_partial_path(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        with Archive(self.output) as archive:
            item = self.prepare_copy(archive)
            gate = Gate()
            step = 0

            def cancel():
                nonlocal step
                step += 1
                if step == 3:
                    raise TaskStopped

            with patch.object(transfers, "BLOCK_SIZE", 16), patch.object(gate, "check", cancel), self.assertRaises(TaskStopped):
                execute_pending(archive, gate, quiet)
            copying = archive.transfer(item.id)
            partial = item.target.parent / copying.staging
            self.assertEqual(copying.state, "copying")
            self.assertEqual(partial.read_bytes(), original[:16])
            self.assertEqual(left.read_bytes(), original)
            self.assertFalse(item.target.exists())
        with Archive(self.output) as archive, patch.object(Path, "unlink") as unlink:
            self.assertEqual(execute_pending(archive, Gate(), quiet), (1, 0))
            unlink.assert_not_called()
            self.assertEqual(partial.read_bytes(), original[:16])
            self.assertEqual(item.target.read_bytes(), original)
            self.assertEqual(left.read_bytes(), original)
            self.assertEqual(execute_pending(archive, Gate(), quiet), (0, 0))

    def test_failed_sync_never_publishes_and_retry_creates_verified_copy(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        with Archive(self.output) as archive:
            item = self.prepare_copy(archive)
            with patch.object(transfers.os, "fsync", side_effect=OSError(ENOSPC, "disque plein")):
                self.assertEqual(execute_pending(archive, Gate(), quiet), (0, 1))
            self.assertFalse(item.target.exists())
            self.assertEqual(left.read_bytes(), original)
            self.assertEqual(archive.transfer(item.id).state, "copying")
            self.assertEqual(execute_pending(archive, Gate(), quiet), (1, 0))
            self.assertEqual(item.target.read_bytes(), original)
            self.assertEqual(left.read_bytes(), original)

    def test_sqlite_transition_failure_preserves_source_and_allows_reconciliation(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        with Archive(self.output) as archive:
            item = self.prepare_copy(archive)
            archive.db.executescript("""
                CREATE TRIGGER test_abort BEFORE UPDATE OF state ON transfers
                WHEN NEW.state = 'verified' BEGIN SELECT RAISE(ABORT, 'interruption'); END;
            """)
            with self.assertRaises(sqlite3.IntegrityError):
                execute_pending(archive, Gate(), quiet)
            self.assertEqual(archive.transfer(item.id).state, "ready")
            self.assertEqual(item.target.read_bytes(), original)
            self.assertEqual(left.read_bytes(), original)
        with Archive(self.output) as archive:
            archive.db.execute("DROP TRIGGER test_abort")
            self.assertEqual(execute_pending(archive, Gate(), quiet), (1, 0))
            self.assertEqual(archive.transfer(item.id).state, "done")
            self.assertEqual(list(item.target.parent.glob("*.png")), [item.target])
            self.assertEqual(item.target.read_bytes(), original)
            self.assertEqual(left.read_bytes(), original)

    def test_source_changes_during_copy(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        observed = "contenu modifié en cours de copie".encode()
        gate = Gate()
        steps = 0

        def modify():
            nonlocal steps
            steps += 1
            if steps == 3:
                left.write_bytes(observed)

        with Archive(self.output) as archive:
            item = self.prepare_copy(archive)
            with patch.object(transfers, "BLOCK_SIZE", 16), patch.object(gate, "check", modify):
                self.assertEqual(execute_pending(archive, gate, quiet), (0, 1))
            row = archive.transfer(item.id)
            self.assertEqual(left.read_bytes(), observed)
            self.assertEqual((item.target.parent / row.staging).read_bytes()[:16], original[:16])
            self.assertFalse(item.target.exists())

    def test_two_processes_cannot_apply_same_journal_concurrently(self):
        left, _ = self.exact_pair()
        original = left.read_bytes()
        child = """
import sys
from pathlib import Path
from multitools.photoarchiviste.inventory import Gate
from multitools.photoarchiviste.journal import Archive
from multitools.photoarchiviste.transfers import execute_pending
try:
    with Archive(Path(sys.argv[1])) as archive:
        assert execute_pending(archive, Gate(), lambda *_: None) == (1, 0)
except OSError as error:
    if "déjà utilisé" not in str(error):
        raise
    sys.exit(7)
"""
        command = [sys.executable, "-B", "-c", child, str(self.output)]
        application = Path(__file__).resolve().parents[1]
        with Archive(self.output) as archive:
            item = self.prepare_copy(archive)
            blocked = subprocess.run(command, cwd=application, capture_output=True, timeout=15)
            self.assertEqual(blocked.returncode, 7, blocked.stderr)
            self.assertFalse(item.target.exists())
        completed = subprocess.run(command, cwd=application, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(left.read_bytes(), original)
        self.assertEqual(item.target.read_bytes(), original)

    def test_large_copy_streams_multiple_blocks_and_opens_as_image(self):
        left = self.left / "grande.bmp"
        Image.new("RGB", (2048, 2048), "orange").save(left)
        with Archive(self.output) as archive, patch.object(Path, "unlink") as unlink:
            item = self.prepare_copy(archive)
            self.assertGreater(item.size, 10 * transfers.BLOCK_SIZE)
            self.assertEqual(execute_pending(archive, Gate(), quiet), (1, 0))
            unlink.assert_not_called()
            self.assertEqual(left.read_bytes(), item.target.read_bytes())
            with Image.open(item.target) as copy:
                copy.load()
                self.assertEqual(copy.size, (2048, 2048))

    def test_network_workspace_does_not_create_lock_or_sqlite(self):
        volume = path_safety.Volume(str(self.output), "partage", False)
        with patch.object(path_safety, "volume_at", return_value=volume):
            with self.assertRaisesRegex(OSError, "système de fichiers local"), Archive(self.output):
                self.fail("journal ouvert sur un partage")
        self.assertEqual(list(self.output.iterdir()), [])

    def test_legacy_moves_remain_blocked_with_validated_source_sorting_panel(self):
        self.exact_pair()
        with Archive(self.output) as archive:
            item = self.prepare_copy(archive)
            with archive.db:
                archive.db.execute("UPDATE transfers SET mode = 'move' WHERE id = ?", (item.id,))
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.addCleanup(root.destroy)
        root.withdraw()
        pane = PhotoArchiviste(root)
        pane.pack()
        pane._launch("Test", lambda *_: "OK", self.output)
        deadline = monotonic() + 5
        while pane.busy and monotonic() < deadline:
            root.update()
            sleep(0.01)
        self.assertFalse(pane.busy)
        self.assertEqual(str(pane.apply_button["state"]), "disabled")
        self.assertEqual(pane.plan_tree.item(str(item.id), "values")[0], "Bloqué · plan ancien")
        self.assertIn("par copie", pane.restore_button["text"])
        self.assertFalse(hasattr(pane, "move"))
        self.assertTrue(any(isinstance(widget, ttk.Label) and "Aucune suppression" in widget["text"]
                            for widget in pane.winfo_children()))
        self.assertTrue(any(pane.tabs.tab(tab,'text')=='Photos · questions et classement' for tab in pane.tabs.tabs()))


if __name__ == "__main__":
    unittest.main()
