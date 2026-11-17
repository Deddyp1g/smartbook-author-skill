import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "smartbook-author" / "scripts" / "browser_verify.py"
HTML = b'''<!doctype html><html><body><button id="next" onclick="document.getElementById('chapter').hidden=false">Next</button><div id="chapter" hidden>Chapter 2</div></body></html>'''


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *args):
        pass


class BrowserVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("Node.js is unavailable")
        probe = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True)
        if probe.returncode:
            raise unittest.SkipTest("Playwright Node.js package is unavailable")
        cls.browser = os.environ.get("SMARTBOOK_TEST_BROWSER")
        if not cls.browser:
            raise unittest.SkipTest("set SMARTBOOK_TEST_BROWSER to a Chromium executable")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / "snapshot.zip"
        with zipfile.ZipFile(self.archive, "w") as zipped:
            zipped.writestr("index.html", HTML)
        sha = "sha256:" + hashlib.sha256(self.archive.read_bytes()).hexdigest()
        (self.root / "manifest.json").write_text(json.dumps({"sha256": sha}), encoding="utf-8")
        self.sha = sha
        self.scenario = self.root / "scenario.json"
        self.scenario.write_text(json.dumps({"steps": [{"name": "chapter", "click": "#next", "expect_visible": "#chapter"}]}), encoding="utf-8")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def run_browser(self):
        return subprocess.run([sys.executable, str(SCRIPT), "--run-dir", str(self.root), "--url", self.url,
                               "--scenario", str(self.scenario), "--browser-executable", self.browser],
                              capture_output=True, text=True, encoding="utf-8", timeout=90)

    def test_real_browser_checks_two_viewports_and_binds_snapshot(self):
        result = self.run_browser()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads((self.root / "browser-report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["sha256"], self.sha)
        self.assertEqual(report["endpoint"], self.url)
        self.assertTrue(all(report["checks"].values()))
        self.assertEqual([view["name"] for view in report["views"]], ["desktop", "mobile"])

    def test_missing_interaction_fails_instead_of_claiming_success(self):
        self.scenario.write_text(json.dumps({"steps": [{"name": "missing", "click": "#missing", "expect_visible": "#chapter"}]}), encoding="utf-8")
        result = self.run_browser()
        self.assertNotEqual(result.returncode, 0)
        report = json.loads((self.root / "browser-report.json").read_text(encoding="utf-8"))
        self.assertFalse(report["checks"]["interaction"])


if __name__ == "__main__":
    unittest.main()
