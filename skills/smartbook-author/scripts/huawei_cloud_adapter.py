#!/usr/bin/env python3
"""Adapter for a separately installed, pinned auto-deploy checkout.

The upstream repository is an external dependency and is not redistributed.
Cloud creation, cleanup, and independent deletion checks need real credentials.
"""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error
import urllib.request

REVISION = "971a801cf0917f2e86365d09b0ed58a93ca27262"
ALLOWED = {
    ("huaweicloud_vpc", "app"), ("huaweicloud_vpc_subnet", "app"),
    ("huaweicloud_networking_secgroup", "app"),
    ("huaweicloud_networking_secgroup_rule", "ssh"),
    ("huaweicloud_networking_secgroup_rule", "http"),
    ("huaweicloud_networking_secgroup_rule", "https"),
    ("huaweicloud_networking_secgroup_rule", "app"),
    ("huaweicloud_networking_secgroup_rule", "egress_all"),
    ("huaweicloud_kps_keypair", "deploy"),
    ("huaweicloud_vpc_eip", "app"),
    ("huaweicloud_compute_instance", "app"),
    ("huaweicloud_compute_eip_associate", "app"),
}


def read_json(path):
    with path.open(encoding="utf-8") as handle:
        result = json.load(handle)
    if not isinstance(result, dict):
        raise ValueError("expected JSON object")
    return result


def run_quiet(command, cwd=None, timeout=1800):
    env = os.environ.copy()
    env.pop("SMARTBOOK_API_KEY", None)
    return subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=False)


def upstream():
    checkout = Path(os.environ["SMARTBOOK_AUTO_DEPLOY"]).expanduser().resolve(strict=True)
    result = run_quiet(["git", "rev-parse", "HEAD"], checkout, 20)
    clean = run_quiet(["git", "status", "--porcelain", "--untracked-files=no"], checkout, 20)
    if result.returncode or result.stdout.decode().strip() != REVISION or clean.returncode or clean.stdout.strip():
        raise ValueError("auto-deploy checkout is not the pinned clean revision")
    for name in ("auto_deploy.py", "destroy.py"):
        if not (checkout / "scripts" / name).is_file():
            raise ValueError("auto-deploy script missing")
    return checkout


def manifest(run_dir):
    value = read_json(run_dir / "manifest.json")
    name = value.get("app_name", "")
    if not re.fullmatch(r"smartbook-[a-z0-9]{12,38}", name):
        raise ValueError("manifest requires a unique smartbook- app_name")
    return value


def state_inventory(run_dir):
    """Refuse state whose resources cannot be attributed to this unique run."""
    name = manifest(run_dir)["app_name"]
    work = run_dir / "auto-deploy"
    if (work / "keypair-recovery.json").exists() or (work / "network-recovery.json").exists():
        raise ValueError("upstream recovery/import detected; ownership uncertain")
    state = read_json(work / "terraform" / "terraform.tfstate")
    found = {}
    for resource in state.get("resources", []):
        if resource.get("mode") != "managed":
            continue
        key = (resource.get("type"), resource.get("name"))
        if resource.get("module") or key not in ALLOWED or key in found:
            raise ValueError("unexpected managed resource")
        instances = resource.get("instances")
        if not isinstance(instances, list) or len(instances) != 1:
            raise ValueError("unexpected resource instances")
        if instances[0].get("deposed") or instances[0].get("status") == "tainted":
            raise ValueError("resource state is ambiguous")
        attrs = instances[0].get("attributes") or {}
        resource_id = attrs.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            raise ValueError("resource id missing")
        found[key] = attrs
    if not found:
        raise ValueError("no state-managed resources")

    named = {
        ("huaweicloud_vpc", "app"): ("name", "auto-deploy-vpc-" + name),
        ("huaweicloud_vpc_subnet", "app"): ("name", "auto-deploy-subnet-" + name),
        ("huaweicloud_networking_secgroup", "app"): ("name", "auto-deploy-sg-" + name),
        ("huaweicloud_kps_keypair", "deploy"): ("name", "auto-deploy-key-" + name),
        ("huaweicloud_compute_instance", "app"): ("name", "auto-deploy-ecs-" + name),
    }
    for key, (field, expected) in named.items():
        if key in found and found[key].get(field) != expected:
            raise ValueError("resource name does not match this run")
    eip = found.get(("huaweicloud_vpc_eip", "app"))
    if eip:
        bandwidth = eip.get("bandwidth")
        if isinstance(bandwidth, list) and len(bandwidth) == 1:
            bandwidth = bandwidth[0]
        if not isinstance(bandwidth, dict) or bandwidth.get("name") != "auto-deploy-bandwidth-" + name:
            raise ValueError("EIP ownership is unclear")
    sg = found.get(("huaweicloud_networking_secgroup", "app"), {}).get("id")
    for (rtype, _), attrs in found.items():
        if rtype == "huaweicloud_networking_secgroup_rule" and (not sg or attrs.get("security_group_id") != sg):
            raise ValueError("security group rule ownership is unclear")
    subnet = found.get(("huaweicloud_vpc_subnet", "app"))
    vpc = found.get(("huaweicloud_vpc", "app"), {}).get("id")
    if subnet and (not vpc or subnet.get("vpc_id") != vpc):
        raise ValueError("subnet ownership is unclear")
    assoc = found.get(("huaweicloud_compute_eip_associate", "app"))
    instance = found.get(("huaweicloud_compute_instance", "app"), {}).get("id")
    if assoc and (not instance or assoc.get("instance_id") != instance or not eip):
        raise ValueError("EIP association ownership is unclear")
    return sorted(f"{rtype}.{item}:{attrs['id']}" for (rtype, item), attrs in found.items())


def deploy(run_dir):
    # This release has no authenticated provider-side deletion verifier.
    # Refuse billable creation until the whole lifecycle is implemented and tested.
    return {"ok": False, "ownership": "uncertain", "resource_ids": [],
            "error": "real_cloud_creation_disabled_missing_deletion_verifier"}

def verify(run_dir):
    data = manifest(run_dir)
    info = read_json(run_dir / "auto-deploy" / "resource-info.json")
    host = str(ipaddress.ip_address(info["public_ip"]))
    endpoint = f"http://{host}:8080/"
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(endpoint, timeout=20) as response:
            observed = response.read(2 * 1024 * 1024 + 1)
        expected = (run_dir / "staging" / "public" / "index.html").read_bytes()
        http_ok = observed == expected
    except (OSError, urllib.error.URLError):
        http_ok = False
    # A separate browser run must describe actual page interactions and console status.
    browser_path = os.environ.get("SMARTBOOK_BROWSER_REPORT")
    browser = read_json(Path(browser_path)) if browser_path else {}
    checks = browser.get("checks", {}) if isinstance(browser.get("checks"), dict) else {}
    bound = browser.get("sha256") == data.get("sha256") and browser.get("endpoint") == endpoint
    output_checks = {key: bound and checks.get(key) is True for key in ("desktop", "mobile", "interaction", "console")}
    output_checks["http"] = http_ok
    return {"ok": all(output_checks.values()), "sha256": data.get("sha256"), "checks": output_checks}


def cleanup(run_dir):
    checkout = upstream()
    ids = state_inventory(run_dir)
    expected = read_json(run_dir / "pipeline.json").get("resource_ids")
    if not isinstance(expected, list) or sorted(expected) != ids:
        raise ValueError("state inventory changed since deployment; refusing destroy")
    result = run_quiet([sys.executable, str(checkout / "scripts" / "destroy.py"),
                        "--workdir", str(run_dir / "auto-deploy" / "terraform"), "--auto-approve"], checkout)
    if result.returncode:
        return {"ok": False, "destroyed_ids": [], "error": "destroy_failed_inspect_retained_workdir"}
    return {"ok": True, "destroyed_ids": ids}


def confirm(run_dir):
    # A user-written report and an empty Terraform state do not independently
    # establish that the provider deleted every billable resource.
    return {"ok": False, "absent_ids": [], "independently_verified": False,
            "error": "provider_api_deletion_verifier_not_available"}


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("deploy", "verify", "cleanup", "confirm"):
        print(json.dumps({"ok": False, "error": "expected phase and run directory"}))
        return 1
    try:
        run_dir = Path(sys.argv[2]).resolve(strict=True)
        result = globals()[sys.argv[1]](run_dir)
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError):
        result = {"ok": False, "ownership": "uncertain", "resource_ids": [],
                  "error": "adapter_failed_inspect_retained_workdir"}
    print(json.dumps(result, ensure_ascii=True))
    # Emit valid failure reports with success exit status so the orchestrator can
    # preserve an ownership result and attempt safe cleanup where possible.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
