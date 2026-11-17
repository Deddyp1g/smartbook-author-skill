import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "skills" / "smartbook-author" / "scripts" / "huawei_cloud_adapter.py"
SPEC = importlib.util.spec_from_file_location("huawei_cloud_adapter", ADAPTER)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)
NAME = "smartbook-123456789abc"


class HuaweiAdapterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        (self.run / "manifest.json").write_text(json.dumps({"app_name": NAME}), encoding="utf-8")
        self.work = self.run / "auto-deploy"
        self.terraform = self.work / "terraform"
        self.terraform.mkdir(parents=True)
        (self.run / "pipeline.json").write_text(json.dumps({"resource_ids": ["example"]}), encoding="utf-8")

    def write_state(self, resource_type="huaweicloud_compute_instance", name="app", attrs=None):
        attrs = attrs or {"id": "ecs-test", "name": "auto-deploy-ecs-" + NAME}
        state = {"resources": [{"mode": "managed", "type": resource_type, "name": name,
                                "instances": [{"attributes": attrs}]}]}
        (self.terraform / "terraform.tfstate").write_text(json.dumps(state), encoding="utf-8")

    def test_deployment_invokes_upstream_and_returns_endpoint(self):
        (self.run / "auto-deploy").rename(self.run / "unused")
        def invoke(command, cwd=None, timeout=1800):
            self.terraform.mkdir(parents=True)
            self.write_state()
            (self.work / "resource-info.json").write_text(json.dumps({"public_ip": "192.0.2.40"}), encoding="utf-8")
            self.assertIn("--no-http", command)
            self.assertEqual(command[command.index("--app-port") + 1], "5000")
            self.assertEqual(command[command.index("--public-cidr") + 1], "192.0.2.1/32")
            return type("Result", (), {"returncode": 0})()
        with patch.dict(os.environ, {"SMARTBOOK_CLOUD_REGION": "cn-north-4",
                                    "SMARTBOOK_CLOUD_CIDR": "192.0.2.1/32"}), \
                patch.object(adapter, "upstream", return_value=self.run), \
                patch.object(adapter, "run_quiet", side_effect=invoke):
            result = adapter.deploy(self.run)
        self.assertTrue(result["ok"])
        self.assertEqual(result["endpoint"], "http://192.0.2.40:5000/")
        self.assertEqual(result["ownership"], "owned")

    def test_provider_confirmation_requires_empty_state(self):
        self.write_state()
        result = adapter.confirm(self.run)
        self.assertFalse(result["ok"])
        self.assertFalse(result["independently_verified"])

    def test_recovery_requires_explicit_confirmation_that_deploy_process_stopped(self):
        self.write_state()
        with patch.dict(os.environ, {"SMARTBOOK_RECOVERY_PROCESSES_STOPPED": ""}):
            result = adapter.recover(self.run)
        self.assertEqual(result["ownership"], "uncertain")
        with patch.dict(os.environ, {"SMARTBOOK_RECOVERY_PROCESSES_STOPPED": "yes"}):
            result = adapter.recover(self.run)
        self.assertTrue(result["ok"])
        self.assertEqual(result["ownership"], "owned")

    def test_failed_deploy_returns_owned_inventory_for_cleanup(self):
        self.work.rename(self.run / "unused")
        def invoke(command, cwd=None, timeout=1800):
            self.terraform.mkdir(parents=True)
            self.write_state()
            return type("Result", (), {"returncode": 1})()
        with patch.dict(os.environ, {"SMARTBOOK_CLOUD_REGION": "cn-north-4",
                                    "SMARTBOOK_CLOUD_CIDR": "192.0.2.1/32"}), \
                patch.object(adapter, "upstream", return_value=self.run), \
                patch.object(adapter, "run_quiet", side_effect=invoke):
            result = adapter.deploy(self.run)
        self.assertFalse(result["ok"])
        self.assertEqual(result["ownership"], "owned")
        self.assertTrue(result["resource_ids"])

    def test_empty_state_is_reported_as_state_evidence_not_provider_verification(self):
        (self.terraform / "terraform.tfstate").write_text(json.dumps({"resources": []}), encoding="utf-8")
        (self.run / "pipeline.json").write_text(json.dumps({"destroyed_ids": ["example"]}), encoding="utf-8")
        result = adapter.confirm(self.run)
        self.assertTrue(result["ok"])
        self.assertEqual(result["absent_ids"], ["example"])
        self.assertEqual(result["verification"], "terraform_state_empty")
        self.assertFalse(result["independently_verified"])

    def test_keypair_recovery_blocks_cleanup_without_invoking_destroy(self):
        self.write_state()
        (self.work / "keypair-recovery.json").write_text("{}", encoding="utf-8")
        with patch.object(adapter, "upstream", return_value=self.run), patch.object(adapter, "run_quiet") as invoke:
            with self.assertRaises(ValueError):
                adapter.cleanup(self.run)
            invoke.assert_not_called()

    def test_network_import_blocks_cleanup_without_invoking_destroy(self):
        self.write_state()
        (self.work / "network-recovery.json").write_text("{}", encoding="utf-8")
        with patch.object(adapter, "upstream", return_value=self.run), patch.object(adapter, "run_quiet") as invoke:
            with self.assertRaises(ValueError):
                adapter.cleanup(self.run)
            invoke.assert_not_called()

    def test_foreign_resource_name_blocks_cleanup_without_invoking_destroy(self):
        self.write_state(attrs={"id": "preexisting-ecs", "name": "existing-production"})
        with patch.object(adapter, "upstream", return_value=self.run), patch.object(adapter, "run_quiet") as invoke:
            with self.assertRaises(ValueError):
                adapter.cleanup(self.run)
            invoke.assert_not_called()

    def test_unknown_managed_resource_blocks_cleanup_without_invoking_destroy(self):
        self.write_state(resource_type="huaweicloud_rds_instance", attrs={"id": "rds-existing"})
        with patch.object(adapter, "upstream", return_value=self.run), patch.object(adapter, "run_quiet") as invoke:
            with self.assertRaises(ValueError):
                adapter.cleanup(self.run)
            invoke.assert_not_called()

    def test_state_changed_after_deploy_blocks_cleanup_without_invoking_destroy(self):
        self.write_state()
        with patch.object(adapter, "upstream", return_value=self.run), patch.object(adapter, "run_quiet") as invoke:
            with self.assertRaises(ValueError):
                adapter.cleanup(self.run)
            invoke.assert_not_called()

    def test_cleanup_retry_accepts_remaining_subset_and_reports_original_inventory(self):
        self.write_state()
        remaining = "huaweicloud_compute_instance.app:ecs-test"
        initial = [remaining, "huaweicloud_kps_keypair.deploy:key-test"]
        (self.run / "pipeline.json").write_text(json.dumps({"resource_ids": initial}), encoding="utf-8")
        with patch.object(adapter, "upstream", return_value=self.run), \
                patch.object(adapter, "run_quiet", return_value=type("Result", (), {"returncode": 0})()) as invoke:
            result = adapter.cleanup(self.run)
        self.assertTrue(result["ok"])
        self.assertEqual(set(result["destroyed_ids"]), set(initial))
        invoke.assert_called_once()

    def test_cleanup_retry_with_empty_state_proceeds_to_confirmation(self):
        initial = ["huaweicloud_compute_instance.app:ecs-test"]
        (self.run / "pipeline.json").write_text(json.dumps({"resource_ids": initial}), encoding="utf-8")
        (self.terraform / "terraform.tfstate").write_text(json.dumps({"resources": []}), encoding="utf-8")
        with patch.object(adapter, "upstream", return_value=self.run), patch.object(adapter, "run_quiet") as invoke:
            result = adapter.cleanup(self.run)
        self.assertTrue(result["ok"])
        self.assertEqual(result["destroyed_ids"], initial)
        invoke.assert_not_called()

    def test_cleanup_retry_rejects_new_resource_id(self):
        self.write_state()
        (self.run / "pipeline.json").write_text(json.dumps({"resource_ids": ["huaweicloud_compute_instance.app:old"]}), encoding="utf-8")
        with patch.object(adapter, "upstream", return_value=self.run), patch.object(adapter, "run_quiet") as invoke:
            with self.assertRaises(ValueError):
                adapter.cleanup(self.run)
        invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
