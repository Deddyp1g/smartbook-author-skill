import hashlib
import http.server
import importlib.util
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
PIPELINE = ROOT / "skills" / "smartbook-author" / "scripts" / "cloud_pipeline.py"
sys.path.insert(0, str(PIPELINE.parent))
SPEC = importlib.util.spec_from_file_location("cloud_pipeline", PIPELINE)
pipeline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pipeline)
SENTINEL = "stpk_test_pipeline_secret_never_deployed"

FAKE_ADAPTER = r'''import json, os, sys
from pathlib import Path
phase, run_dir = sys.argv[1:]
with Path(os.environ["PIPELINE_TEST_EVENTS"]).open("a", encoding="utf-8") as events:
    events.write(phase + "\n")
if "SMARTBOOK_API_KEY" in os.environ:
    print(json.dumps({"ok": False, "error": "author credential leaked"}))
    sys.exit(0)
scenario = os.environ.get("PIPELINE_TEST_SCENARIO", "success")
records = {
    "deploy": {"ok": scenario != "deploy_fail", "ownership": "uncertain" if scenario == "foreign" else "owned", "resource_ids": ["test-ecs-1"], "endpoint": "http://127.0.0.1:1"},
    "verify": {"ok": scenario != "verify_fail", "sha256": os.environ["PIPELINE_TEST_SHA256"], "checks": {"http": True, "desktop": True, "mobile": True, "interaction": True, "console": True}},
    "cleanup": {"ok": scenario != "cleanup_fail", "destroyed_ids": [] if scenario == "missing_destroy" else ["test-ecs-1"]},
    "confirm": {"ok": scenario != "confirm_fail", "absent_ids": [] if scenario == "missing_absent" else ["test-ecs-1"], "independently_verified": scenario != "confirm_unverified"},
}
if scenario == "hash_mismatch" and phase == "verify":
    records[phase]["sha256"] = "0" * 64
print(json.dumps(records[phase]))
'''


class UploadHandler(http.server.BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        type(self).calls.append(self.path)
        self.send_response(500)
        self.end_headers()


class CloudPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.archive = self.directory / "book.zip"
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr("index.html", "<html><body>Chapter one</body></html>")
        self.run_dir = self.directory / "run"
        self.environment = dict(os.environ, SMARTBOOK_API_KEY=SENTINEL)
        self.events = self.directory / "events.txt"
        self.adapter = self.directory / "adapter.py"
        self.adapter.write_text(FAKE_ADAPTER, encoding="utf-8")
        self.environment.update(PIPELINE_TEST_EVENTS=str(self.events),
                                PIPELINE_TEST_SHA256="sha256:" + hashlib.sha256(self.archive.read_bytes()).hexdigest())

    def cli(self, *args):
        return subprocess.run([sys.executable, str(PIPELINE), *map(str, args)],
                              env=self.environment, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    def test_prepare_isolates_validated_archive_and_does_not_embed_author_key(self):
        original = self.archive.read_bytes()
        result = self.cli("prepare", self.archive, "--run-dir", self.run_dir)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.archive.read_bytes(), original)
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)
        files = [path for path in self.run_dir.rglob("*") if path.is_file()]
        self.assertTrue(files)
        for path in files:
            self.assertNotIn(SENTINEL.encode(), path.read_bytes(), str(path))
        self.assertTrue(any(hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(original).digest()
                            for path in files), "the exact ZIP snapshot must be preserved")

    def test_prepare_rejects_invalid_archive_before_cloud_work(self):
        self.archive.write_bytes(b"not a zip archive")
        result = self.cli("prepare", self.archive, "--run-dir", self.run_dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(SENTINEL, result.stdout + result.stderr)

    def test_prepare_does_not_overwrite_existing_run(self):
        self.run_dir.mkdir()
        marker = self.run_dir / "existing-file"
        marker.write_text("must stay intact", encoding="utf-8")
        result = self.cli("prepare", self.archive, "--run-dir", self.run_dir)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(encoding="utf-8"), "must stay intact")

    def prepare(self):
        result = self.cli("prepare", self.archive, "--run-dir", self.run_dir)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def execute(self, scenario):
        self.environment["PIPELINE_TEST_SCENARIO"] = scenario
        return self.cli("execute", "--run-dir", self.run_dir, "--adapter", self.adapter,
                        "--simulation")

    def observed_phases(self):
        return self.events.read_text(encoding="utf-8").splitlines() if self.events.exists() else []

    def assert_upload_blocked(self):
        UploadHandler.calls = []
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), UploadHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = "http://127.0.0.1:" + str(server.server_port)
            result = self.cli("upload", "--run-dir", self.run_dir, "--book-id", "7", "--base-url", url)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(UploadHandler.calls, [])
            self.assertNotIn(SENTINEL, result.stdout + result.stderr)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_simulated_success_cleans_and_confirms_but_cannot_upload(self):
        self.prepare()
        result = self.execute("success")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.observed_phases(), ["deploy", "verify", "cleanup", "confirm"])
        self.assert_upload_blocked()

    def test_failed_verification_still_cleans_and_cannot_upload(self):
        self.prepare()
        result = self.execute("verify_fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cleanup", self.observed_phases())
        self.assert_upload_blocked()

    def test_failed_deployment_with_owned_resource_still_cleans(self):
        self.prepare()
        result = self.execute("deploy_fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cleanup", self.observed_phases())
        self.assert_upload_blocked()

    def test_uncertain_ownership_never_destroys_resources(self):
        self.prepare()
        result = self.execute("foreign")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("cleanup", self.observed_phases())
        self.assert_upload_blocked()

    def test_hash_mismatch_and_incomplete_cleanup_never_upload(self):
        for scenario in ("hash_mismatch", "cleanup_fail", "missing_destroy", "confirm_fail", "missing_absent", "confirm_unverified"):
            with self.subTest(scenario=scenario):
                self.run_dir = self.directory / scenario
                self.events = self.directory / (scenario + ".events")
                self.environment["PIPELINE_TEST_EVENTS"] = str(self.events)
                self.prepare()
                result = self.execute(scenario)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_upload_blocked()

    def test_custom_adapter_requires_explicit_simulation(self):
        self.prepare()
        result = self.cli("execute", "--run-dir", self.run_dir, "--adapter", self.adapter)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.observed_phases(), [])
        self.assert_upload_blocked()

    def test_modified_staging_is_rejected_before_deploy(self):
        self.prepare()
        (self.run_dir / "staging" / "public" / "index.html").write_text("modified", encoding="utf-8")
        result = self.execute("success")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.observed_phases(), [])
        self.assert_upload_blocked()

    def test_modified_snapshot_is_rejected_before_deploy(self):
        self.prepare()
        with zipfile.ZipFile(self.run_dir / "snapshot.zip", "w") as archive:
            archive.writestr("index.html", "<html><body>different chapter</body></html>")
        result = self.execute("success")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.observed_phases(), [])
        self.assert_upload_blocked()

    def test_unknown_upload_reuses_key_and_preserves_target(self):
        self.prepare()
        state_path = self.run_dir / "pipeline.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        sha = state["sha256"]
        # Construct a local unit-test fixture; this is not evidence of cloud deletion.
        state.update(status="upload_ready", simulation=False, resource_ids=["unit-test"],
                     verification={"ok": True, "sha256": sha, "checks": dict.fromkeys(pipeline.CHECKS, True)},
                     cleanup={"destroyed_ids": ["unit-test"], "absent_ids": ["unit-test"],
                              "independently_verified": True})
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with patch.object(pipeline, "upload", return_value={"ok": False, "error": "upload_outcome_unknown"}) as send:
            first = pipeline.upload_ready(self.run_dir, 7, "http://127.0.0.1:1")
            self.assertFalse(first["ok"])
            first_key = send.call_args.args[4]
            pipeline.upload_ready(self.run_dir, 7, "http://127.0.0.1:1")
            self.assertEqual(send.call_args.args[4], first_key)
            with self.assertRaises(ValueError):
                pipeline.upload_ready(self.run_dir, 7, "http://127.0.0.1:1", "different-key")
            with self.assertRaises(ValueError):
                pipeline.upload_ready(self.run_dir, 8, "http://127.0.0.1:1")
            self.assertEqual(send.call_count, 2)

    def staged(self, command, *args):
        return self.cli(command, "--run-dir", self.run_dir, *args)

    def start_staged(self, scenario="success"):
        self.environment["PIPELINE_TEST_SCENARIO"] = scenario
        return self.staged("staged-start", "--adapter", self.adapter)

    def test_staged_run_pauses_for_verification_then_cleans(self):
        self.prepare()
        result = self.start_staged()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["endpoint"], "http://127.0.0.1:1")
        self.assertEqual(self.observed_phases(), ["deploy"])
        self.assert_upload_blocked()
        result = self.staged("staged-verify")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.observed_phases(), ["deploy", "verify"])
        result = self.staged("staged-cleanup")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.observed_phases(), ["deploy", "verify", "cleanup", "confirm"])
        self.assert_upload_blocked()

    def test_staged_verification_failure_can_still_cleanup(self):
        self.prepare()
        self.assertEqual(self.start_staged("verify_fail").returncode, 0)
        self.assertNotEqual(self.staged("staged-verify").returncode, 0)
        self.assertEqual(self.staged("staged-cleanup").returncode, 0)
        self.assertEqual(self.observed_phases(), ["deploy", "verify", "cleanup", "confirm"])
        self.assert_upload_blocked()

    def test_staged_cleanup_after_confirm_failure_only_rechecks_confirmation(self):
        self.prepare()
        self.assertEqual(self.start_staged("confirm_fail").returncode, 0)
        self.assertEqual(self.staged("staged-verify").returncode, 0)
        self.assertNotEqual(self.staged("staged-cleanup").returncode, 0)
        self.environment["PIPELINE_TEST_SCENARIO"] = "success"
        result = self.staged("staged-cleanup")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.observed_phases(), ["deploy", "verify", "cleanup", "confirm", "confirm"])
        self.assert_upload_blocked()

    def test_staged_cleanup_from_interrupted_verification(self):
        self.prepare()
        self.assertEqual(self.start_staged().returncode, 0)
        state_path = self.run_dir / "pipeline.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["status"] = "verifying"  # Simulate process interruption after the durable state transition.
        state_path.write_text(json.dumps(state), encoding="utf-8")
        result = self.staged("staged-cleanup")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.observed_phases(), ["deploy", "cleanup", "confirm"])
        self.assert_upload_blocked()

    def test_staged_cleanup_refuses_unconfirmed_ownership(self):
        self.prepare()
        self.assertNotEqual(self.start_staged("foreign").returncode, 0)
        self.assertNotEqual(self.staged("staged-cleanup").returncode, 0)
        self.assertEqual(self.observed_phases(), ["deploy"])
        self.assert_upload_blocked()

    def test_staged_commands_refuse_adapter_modified_since_deploy(self):
        self.prepare()
        self.assertEqual(self.start_staged().returncode, 0)
        self.adapter.write_text(FAKE_ADAPTER + "\n# modified\n", encoding="utf-8")
        self.assertNotEqual(self.staged("staged-verify").returncode, 0)
        self.assertNotEqual(self.staged("staged-cleanup").returncode, 0)
        self.assertEqual(self.observed_phases(), ["deploy"])
        self.assert_upload_blocked()


if __name__ == "__main__":
    unittest.main()
