import contextlib
import hashlib
import http.client
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "smartbook-author" / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("smartbook_upload", SCRIPTS / "upload.py")
upload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upload)
KEY = "stpk_test_secret_123456789"
ASSET = "12345678-1234-4234-8234-123456789abc"


class Handler(http.server.BaseHTTPRequestHandler):
    calls = []
    response_status = 201
    response_body = None
    redirect_url = None

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.__class__.calls.append((self.path, dict(self.headers), body))
        result = self.response_body
        if result is None:
            result = json.dumps({"id": ASSET, "kind": "smartbook", "status": "pending_review",
                                 "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                                 "size_bytes": len(body), "published": False}).encode()
        self.send_response(self.response_status)
        if self.redirect_url:
            self.send_header("Location", self.redirect_url)
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)


@contextlib.contextmanager
def server():
    Handler.calls = []
    Handler.response_status = 201
    Handler.response_body = None
    Handler.redirect_url = None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(httpd.server_port)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Path(self.temp.name) / "book.zip"
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("index.html", "<html><body>chapter</body></html>")

    def send(self, url, **kwargs):
        return upload.upload(self.archive, 7, url, KEY, "stable-001", **kwargs)

    def test_uploads_exact_validated_bytes_to_requested_book(self):
        with server() as url:
            result = self.send(url)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["status"], "pending_review")
            self.assertEqual(result["asset_id"], ASSET)
            self.assertEqual(len(Handler.calls), 1)
            path, headers, body = Handler.calls[0]
            self.assertEqual(path, "/v1/author/books/7/media/smartbook")
            self.assertEqual(headers["Authorization"], "Bearer " + KEY)
            self.assertEqual(headers["Content-Type"], "application/zip")
            self.assertEqual(headers["Idempotency-Key"], "stable-001")
            self.assertEqual(body, self.archive.read_bytes())

    def test_validation_and_credentials_fail_before_network(self):
        with server() as url:
            self.archive.write_bytes(b"bad zip")
            with self.assertRaises(ValueError):
                self.send(url)
            with self.assertRaises(ValueError):
                upload.upload(self.archive, 0, url, KEY, "k")
            with self.assertRaises(ValueError):
                upload.upload(self.archive, 7, url, "bad", "k")
            self.assertEqual(Handler.calls, [])

    def test_origin_policy(self):
        for value in ("https://user:pass@example.com", "https://example.com/path",
                      "https://example.com/?x=1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                upload.origin_url(value)
        self.assertEqual(upload.origin_url("http://example.com"), "http://example.com")
        self.assertEqual(upload.origin_url("http://127.0.0.1:15173"), "http://127.0.0.1:15173")

    def test_redirect_is_not_followed_and_response_body_not_printed(self):
        with server() as url:
            Handler.response_status = 307
            Handler.redirect_url = url + "/elsewhere"
            Handler.response_body = KEY.encode()
            result = self.send(url)
            self.assertFalse(result["ok"])
            self.assertEqual(result["http_status"], 307)
            self.assertEqual(len(Handler.calls), 1)
            self.assertNotIn(KEY, json.dumps(result))

    def test_rejected_timeout_and_invalid_response_preserve_idempotency(self):
        with server() as url:
            Handler.response_status = 403
            Handler.response_body = KEY.encode()
            result = self.send(url)
            self.assertEqual(result["http_status"], 403)
            self.assertEqual(result["idempotency_key"], "stable-001")
            Handler.response_status = 201
            Handler.response_body = b"{\"id\": \"untrusted\"}"
            result = self.send(url)
            self.assertEqual(result["error"], "unverified_server_response")
        with patch.object(upload.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = TimeoutError("secret text")
            result = self.send("http://127.0.0.1:1234")
            self.assertEqual(result["error"], "upload_outcome_unknown")
            self.assertEqual(result["idempotency_key"], "stable-001")
            self.assertNotIn(KEY, json.dumps(result))

    def test_known_error_code_only_and_http_disconnect(self):
        with server() as url:
            Handler.response_status = 400
            Handler.response_body = json.dumps({"error": {"code": "unsupported_reader_css", "detail": KEY}}).encode()
            self.assertEqual(self.send(url)["server_code"], "unsupported_reader_css")
            Handler.response_body = json.dumps({"error": {"code": KEY, "detail": KEY}}).encode()
            result = self.send(url)
            self.assertNotIn("server_code", result)
            self.assertNotIn(KEY, json.dumps(result))
        with patch.object(upload.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = http.client.IncompleteRead(b"partial")
            result = self.send("http://127.0.0.1:1234")
            self.assertEqual(result["error"], "upload_outcome_unknown")
            self.assertEqual(result["idempotency_key"], "stable-001")

    def test_cli_never_prints_key_on_rejected_response(self):
        with server() as url:
            Handler.response_status = 403
            Handler.response_body = KEY.encode()
            env = dict(os.environ, SMARTBOOK_API_KEY=KEY)
            result = subprocess.run([sys.executable, str(SCRIPTS / "upload.py"), str(self.archive),
                                     "--book-id", "7", "--base-url", url,
                                     "--idempotency-key", "stable-001"],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(KEY, result.stdout + result.stderr)
            self.assertIn("stable-001", result.stdout)
            self.assertEqual(len(Handler.calls), 1)

    def test_cli_supports_remote_http_with_warning(self):
        args = ["upload.py", str(self.archive), "--book-id", "7", "--base-url", "http://example.com:5173"]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", args), patch.dict(os.environ, {"SMARTBOOK_API_KEY": KEY}), \
                patch.object(upload, "upload", return_value={"ok": True}) as send, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(upload.main(), 0)
        self.assertEqual(send.call_args.args[2], "http://example.com:5173")
        self.assertIn("HTTP exposes", err.getvalue())
        self.assertNotIn(KEY, out.getvalue() + err.getvalue())

    def test_validates_and_uploads_same_snapshot(self):
        with server() as url:
            original = self.archive.read_bytes()
            actual_validate = upload.validate
            def mutate_after_snapshot(path):
                self.archive.write_bytes(b"changed")
                return actual_validate(path)
            with patch.object(upload, "validate", mutate_after_snapshot):
                result = self.send(url)
            self.assertTrue(result["ok"], result)
            self.assertEqual(Handler.calls[0][2], original)


if __name__ == "__main__":
    unittest.main()
