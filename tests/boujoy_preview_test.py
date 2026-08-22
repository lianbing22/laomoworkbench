"""Preview route: agent-built files under the gateway cwd.

The SPA fallback used to swallow /Users/.../file.html URLs and render the app
shell ("生成的网页没有 CSS"). /api/preview serves real files, confined to the
server working directory, with a sandbox CSP so generated pages can never call
the gateway APIs."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import boujoy_server as bs  # noqa: E402


class PreviewRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_cwd = os.getcwd()
        cls._tmp = tempfile.TemporaryDirectory(prefix="laomo-preview-")
        cls.root = Path(cls._tmp.name)
        os.chdir(cls.root)
        (cls.root / "page.html").write_text("<!doctype html><h1>产物</h1>", "utf-8")
        (cls.root / "sub").mkdir()
        (cls.root / "sub" / "note.md").write_text("# note", "utf-8")
        outside = Path(tempfile.mkdtemp(prefix="laomo-preview-out-"))
        cls._outside = outside
        (outside / "secret.txt").write_text("secret", "utf-8")
        vault = cls.root / "vault"
        static = cls_root_static = cls.root / "static"
        vault.mkdir(exist_ok=True)
        static.mkdir(exist_ok=True)
        config = bs.AppConfig(vault, static)
        cls.server = bs.BoujoyServer(("127.0.0.1", 0), config)
        cls.port = cls.server.server_address[1]
        cls._thread = threading.Thread(target=cls.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.chdir(cls._old_cwd)
        cls._tmp.cleanup()
        import shutil
        shutil.rmtree(cls._outside, ignore_errors=True)

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_serves_file_with_sandbox_csp(self):
        status, headers, body = self._get("/api/preview?path=page.html")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"<h1>", body)
        self.assertIn("sandbox", headers.get("Content-Security-Policy", ""))

    def test_serves_nested_relative_path(self):
        status, _, body = self._get("/api/preview?path=sub/note.md")
        self.assertEqual(status, 200)
        self.assertIn(b"# note", body)

    def test_rejects_escape_outside_cwd(self):
        secret = self._outside / "secret.txt"
        for probe in (f"../{secret.parent.name}/secret.txt", str(secret)):
            status, _, _ = self._get(f"/api/preview?path={urllib.parse.quote(probe)}")
            self.assertEqual(status, 403, probe)

    def test_rejects_system_paths(self):
        status, _, _ = self._get("/api/preview?path=/etc/passwd")
        self.assertEqual(status, 403)

    def test_directory_lists_entries(self):
        status, headers, body = self._get("/api/preview?path=")
        self.assertEqual(status, 200)
        self.assertIn(b"page.html", body)
        self.assertIn(b"sub/", body)
        self.assertNotIn(b".laomo", body)  # dot entries stay hidden

    def test_missing_file_404(self):
        status, _, _ = self._get("/api/preview?path=nope.html")
        self.assertEqual(status, 404)

    def test_absolute_path_paste_after_origin(self):
        # Users paste /Users/.../file.html after the origin; that used to hit
        # the SPA fallback and render the app shell ("页面没有 CSS").
        pasted = f"{self.root}/page.html"
        status, headers, body = self._get(pasted)
        self.assertEqual(status, 200)
        self.assertIn(b"<h1>", body)
        self.assertIn("sandbox", headers.get("Content-Security-Policy", ""))

    def test_absolute_path_outside_cwd_rejected(self):
        outside = self._outside / "secret.txt"
        status, _, _ = self._get(str(outside))
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
