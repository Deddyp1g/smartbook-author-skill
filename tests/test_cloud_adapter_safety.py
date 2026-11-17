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

    def test_real_deployment_is_disabled_before_independent_provider_check_exists(self):
        # Cloud verifier is not implemented: no cloud resources may be created.
        # Even a fully plausible configuration must not invoke upstream at all.
        (self.run / "auto-deploy").rename(self.run / "unused")
        with patch.dict(os.environ, {"SMARTBOOK_CLOUD_REGION": "cn-north-4",
                                    "SMARTBOOK_CLOUD_CIDR": "192.0.2.1/32"}), \
                patch.object(adapter, "upstream", return_value=self.run) as upstream, \
                patch.object(adapter, "run_quiet") as invoke:
            try:
                result = adapter.deploy(self.run)
            except (ValueError, RuntimeError):
                result = {"ok": False}
            self.assertFalse(result.get("ok", False))
            upstream.assert_not_called()
            invoke.assert_not_called()

    def test_provider_confirmation_cannot_be_simulated_by_empty_state(self):
        self.write_state()
        result = adapter.confirm(self.run)
        self.assertFalse(result["ok"])
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


if __name__ == "__main__":
    unittest.main()
