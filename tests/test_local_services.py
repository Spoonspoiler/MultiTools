from contextlib import contextmanager
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from PIL import Image

from multitools.assistant_ui import verified_answer
from multitools.documents import Documents, research
from multitools.kiwix import ArchiveFile, KiwixAPI, KiwixServer, inventory
from multitools.local_http import Cancelled, HttpStatusError, LocalHTTP, LocalServiceError
from multitools.ollama import DEFAULT_MODEL, Ollama
from multitools.photoarchiviste.inventory import Gate
from multitools.storage import backup_independence, guard_target, parse_mounts, plan_backup


class FakeOllamaHTTP:
    def __init__(self):
        self.calls = []

    def json(self, path, data=None, **_):
        self.calls.append((path, data))
        if path == "/api/tags":
            return {"models": [{"name": DEFAULT_MODEL}, {"name": "vision:latest"},
                               {"name": "test:cloud"}, {"name": "proxy:latest", "remote_host": "ollama.com"}]}
        if path == "/api/show":
            return {"capabilities": ["vision", "thinking"] if data["model"] == DEFAULT_MODEL else []}
        if path == "/api/ps":
            return {"models": [{"name": DEFAULT_MODEL, "size": 12345}]}
        if path == "/api/generate":
            return {"done": True}
        raise AssertionError(path)

    @contextmanager
    def open(self, path, data, **_):
        self.calls.append((path, data))
        lines = [{"message": {"content": "Bon"}}, {"message": {"content": "jour"}}, {"done": True, "eval_count": 2}]
        yield BytesIO("\n".join(json.dumps(line) for line in lines).encode())


def quiet(*_):
    pass


class ServiceTests(TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="services-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name).resolve()

    def test_local_http_rejects_external_addresses_and_redirects(self):
        for host in ("8.8.8.8", "0.0.0.0", "example.org"):
            with self.assertRaises(ValueError):
                LocalHTTP(host, 8080, lan=True)
        with self.assertRaises(ValueError):
            Ollama("192.168.3.2")
        http = LocalHTTP("192.168.3.2", 8080, lan=True)
        with self.assertRaises(ValueError):
            http.read("//example.org/x")
        con = Mock()
        response = Mock(status=302, reason="Redirect")
        con.getresponse.return_value.__enter__ = Mock(return_value=response)
        con.getresponse.return_value.__exit__ = Mock(return_value=False)
        with patch("multitools.local_http.HTTPConnection", return_value=con), self.assertRaises(HttpStatusError):
            http.read("/search")
        con.request.assert_called_once()

    def test_streamed_generation_vision_and_model_switch(self):
        client = Ollama()
        client.http = FakeOllamaHTTP()
        self.assertEqual(client.models(), [DEFAULT_MODEL, "vision:latest"])
        photo = self.directory / "aperçu.png"
        Image.new("RGB", (700, 500), "green").save(photo)
        emitted = []
        messages = [{"role": "user", "content": "Bonjour"}]
        result = client.chat(DEFAULT_MODEL, messages, emitted.append, Event(), image=photo)
        self.assertEqual((emitted, result.text, result.model_bytes), (["Bon", "jour"], "Bonjour", 12345))
        body = next(data for path, data in client.http.calls if path == "/api/chat")
        self.assertEqual(body["options"]["num_ctx"], 2048)
        self.assertEqual(body["keep_alive"], "2m")
        self.assertTrue(body["messages"][-1]["images"])
        self.assertNotIn("images", messages[-1])
        with self.assertRaises(LocalServiceError):
            client.chat("vision:latest", messages, quiet, Event(), image=photo)
        client.chat("vision:latest", messages, quiet, Event())
        self.assertIn(("/api/generate", {"model": DEFAULT_MODEL, "keep_alive": 0, "stream": False}), client.http.calls)

    def test_cancelled_stream_does_not_finish(self):
        client = Ollama()
        client.http = FakeOllamaHTTP()
        cancel = Event()
        with self.assertRaises(Cancelled):
            client.chat(DEFAULT_MODEL, [{"role": "user", "content": "Bonjour"}], lambda _: cancel.set(), cancel)
        self.assertNotIn("/api/ps", [path for path, _ in client.http.calls])

    def test_document_retrieval_without_services_and_stale_sources(self):
        root = self.directory / "notices"
        root.mkdir()
        manual = root / "Notice.txt"
        manual.write_text("Pression hydraulique. Purger la conduite en suivant le manuel.", encoding="utf-8")
        docs = Documents(self.directory / "index.sqlite3")
        self.assertEqual(docs.index([root], Gate(), quiet), 1)
        self.assertEqual(docs.index([root], Gate(), quiet), 0)
        hits = research("Explique la pression hydraulique", docs, [root], KiwixServer(), None, Event(), quiet)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].url, manual.as_uri())
        self.assertIn("Purger", hits[0].excerpt)
        self.assertEqual(verified_answer("La notice décrit une purge [S1].", hits), "La notice décrit une purge [S1].")
        with self.assertRaises(LocalServiceError):
            verified_answer("Une autre notice [S9].", hits)
        manual.write_text("Une autre version", encoding="utf-8")
        self.assertFalse(docs.search("purger", roots=[root]))

    def test_index_keeps_completed_files_after_cancellation(self):
        (self.directory / "a.md").write_text("Guidage de navigation", encoding="utf-8")
        (self.directory / "b.md").write_text("Cartes de navigation", encoding="utf-8")
        docs = Documents(self.directory / "index.sqlite3")
        gate = Gate()
        from multitools.photoarchiviste.inventory import TaskStopped
        with self.assertRaises(TaskStopped):
            docs.index([self.directory], gate, lambda _: gate.stopped.set())
        self.assertEqual(len(docs.search("navigation")), 1)
        self.assertEqual(docs.index([self.directory], Gate(), quiet), 1)
        self.assertEqual(len(docs.search("navigation")), 2)

    def test_kiwix_catalog_rss_and_raw_data(self):
        api = KiwixAPI("192.168.3.2", 8080)
        api.http.read = Mock(side_effect=[
            b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Guides</title><language>fra</language>'
            b'<link type="text/html" href="/content/guides" /></entry></feed>',
            b'<rss><channel><item><title>Purge</title><link>/content/guides/A/Purge</link>'
            b'<description>Une <b>purge</b> du circuit</description></item><item><title>Externe</title>'
            b'<link>http://example.org/content/other/a</link></item></channel></rss>',
            b'<p>Pour la purge du circuit, consulter ce paragraphe de la notice.</p>',
        ])
        book, = api.books()
        hit, = api.search("purge circuit", book)
        self.assertEqual((book.name, book.language), ("guides", "fra"))
        self.assertEqual(hit.url, "http://192.168.3.2:8080/content/guides/A/Purge")
        self.assertEqual(hit.excerpt, "Une purge du circuit")
        self.assertIn("notice", api.extract(hit, ["purge"]))
        self.assertTrue(api.http.read.call_args.args[0].startswith("/raw/guides/content/A/"))

    def test_kiwix_missing_binary_and_managed_start_arguments(self):
        archive = self.directory / "guide.zim"
        archive.write_bytes(b"example")
        scanned = inventory([self.directory], [archive])
        self.assertEqual([(item.path, item.size) for item in scanned], [(archive, 7)])
        service = KiwixServer()
        with patch.object(service, "executable", return_value=None), self.assertRaises(LocalServiceError):
            service.start("127.0.0.1", 8080, scanned)
        child = Mock(stderr=StringIO(), poll=Mock(return_value=None))
        command = Mock(return_value=child)
        help_text = Mock(stdout="--address --blockexternal --threads --searchLimit --attachToProcess", stderr="")
        version = Mock(stdout="kiwix-serve 3.5.0", stderr="")
        with (patch.object(service, "executable", return_value="kiwix-serve"),
              patch("multitools.kiwix.socket.create_connection", side_effect=ConnectionRefusedError),
              patch("multitools.kiwix.subprocess.run", side_effect=[help_text, version]),
              patch("multitools.kiwix.subprocess.Popen", command), patch.object(KiwixAPI, "books", return_value=[])):
            self.assertEqual(service.start("192.168.3.2", 8080, scanned), [])
            args = command.call_args.args[0]
            self.assertIn("--address=192.168.3.2", args)
            self.assertIn("--blockexternal", args)
            self.assertEqual(args[-1], str(archive))
            service.stop()
            child.terminate.assert_called_once()

    def test_mount_uuid_and_physical_backup(self):
        data, same_disk, backup = [self.directory / name for name in ("DATA", "partition", "BACKUP")]
        for path in (data, same_disk, backup):
            path.mkdir()
        def partition(path, uuid):
            return {"name": f"/dev/{uuid}", "type": "part", "mountpoints": [str(path)], "uuid": uuid}
        found = parse_mounts({"blockdevices": [{"name": "/dev/sda", "type": "disk", "children": [partition(data, "a1"), partition(same_disk, "a2")]},
                                            {"name": "/dev/sdb", "type": "disk", "children": [partition(backup, "b1")]}]})
        by_path = {mount.path: mount for mount in found}
        self.assertIn("partagé", backup_independence(by_path[data], by_path[same_disk]))
        roles = {"DATA": {"path": str(data), "uuid": "a1"}, "BACKUP": {"path": str(backup), "uuid": "b1"}}
        self.assertEqual(plan_backup(roles, found, "restic").target, backup)
        with patch("multitools.storage.mounts", return_value=[]), self.assertRaises(OSError):
            guard_target(data / "nouvelle-photo.jpg", 100, roles)
