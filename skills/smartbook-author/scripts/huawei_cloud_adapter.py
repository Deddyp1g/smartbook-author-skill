#!/usr/bin/env python3
"""Adapter for a pinned, separately obtained auto-deploy checkout.

The upstream repository is an external dependency and is not redistributed.
Cloud creation and cleanup require Huawei Cloud credentials. Terraform state
does not constitute an independent provider-side deletion check.
"""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REVISION = "971a801cf0917f2e86365d09b0ed58a93ca27262"
UPSTREAM_URL = "https://gitcode.com/weixin_43883336/auto-deploy.git"
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


class DependencyError(ValueError):
    """A dependency could not be obtained or verified."""


def validate_checkout(checkout):
    result = run_quiet(["git", "rev-parse", "HEAD"], checkout, 20)
    clean = run_quiet(["git", "status", "--porcelain", "--untracked-files=no"], checkout, 20)
    if result.returncode or result.stdout.decode().strip() != REVISION or clean.returncode or clean.stdout.strip():
        raise DependencyError("auto-deploy checkout is not the required clean revision; inspect the configured path")
    for name in ("auto_deploy.py", "destroy.py"):
        if not (checkout / "scripts" / name).is_file():
            raise DependencyError("auto-deploy checkout is incomplete; inspect the configured path")
    return checkout


def git_bootstrap(command, cwd=None):
    env = os.environ.copy()
    env.pop("SMARTBOOK_API_KEY", None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300, check=False)
    except FileNotFoundError as exc:
        raise DependencyError("Git is required to download auto-deploy; install Git and retry") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DependencyError("could not download auto-deploy; check Git and network access and retry") from exc
    if result.returncode:
        raise DependencyError("could not download the pinned auto-deploy revision; check GitCode access and retry")


def upstream():
    override = os.environ.get("SMARTBOOK_AUTO_DEPLOY")
    if override:
        try:
            return validate_checkout(Path(override).expanduser().resolve(strict=True))
        except (OSError, subprocess.SubprocessError) as exc:
            raise DependencyError("cannot access SMARTBOOK_AUTO_DEPLOY; check its path and Git installation") from exc

    cache = Path(os.environ.get("SMARTBOOK_CACHE_DIR", Path.home() / ".cache" / "smartbook-author"))
    cache = cache.expanduser().resolve()
    checkout = cache / ("auto-deploy-" + REVISION)
    lock = cache / ("auto-deploy-" + REVISION + ".lock")
    try:
        cache.mkdir(parents=True, exist_ok=True)
        if checkout.exists() or checkout.is_symlink():
            return validate_checkout(checkout)

        # A separate lock directory prevents simultaneous installations from
        # replacing each other; existing cache entries are never overwritten.
        deadline = time.monotonic() + 60
        while True:
            try:
                lock.mkdir()
                break
            except FileExistsError:
                if checkout.exists() or checkout.is_symlink():
                    return validate_checkout(checkout)
                if time.monotonic() >= deadline:
                    raise DependencyError("auto-deploy installation is locked; wait or inspect the cache lock")
                time.sleep(0.25)
        try:
            if checkout.exists() or checkout.is_symlink():
                return validate_checkout(checkout)
            with tempfile.TemporaryDirectory(prefix=".auto-deploy-download-", dir=cache) as temporary:
                candidate = Path(temporary) / "checkout"
                git_bootstrap(["git", "clone", "--no-checkout", "--", UPSTREAM_URL, str(candidate)])
                git_bootstrap(["git", "checkout", "--detach", REVISION], cwd=candidate)
                validate_checkout(candidate)
                if checkout.exists() or checkout.is_symlink():
                    return validate_checkout(checkout)
                candidate.rename(checkout)
            return checkout
        finally:
            lock.rmdir()
    except DependencyError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise DependencyError("cannot prepare auto-deploy cache; check Git and cache directory permissions") from exc


def manifest(run_dir):
    value = read_json(run_dir / "manifest.json")
    name = value.get("app_name", "")
    if not re.fullmatch(r"smartbook-[a-z0-9]{12,38}", name):
        raise ValueError("manifest requires a unique smartbook- app_name")
    return value


def state_inventory(run_dir, expected_ids=None):
    """Refuse state whose resources cannot be attributed to this unique run."""
    name = manifest(run_dir)["app_name"]
    work = run_dir / "auto-deploy"
    if (work / "keypair-recovery.json").exists() or (work / "network-recovery.json").exists():
        raise ValueError("upstream recovery/import detected; ownership uncertain")
    state = read_json(work / "terraform" / "terraform.tfstate")
    found = {}
    resources = state.get("resources")
    if not isinstance(resources, list):
        raise ValueError("invalid state inventory")
    for resource in resources:
        if resource.get("mode") != "managed":
            continue
        key = (resource.get("type"), resource.get("name"))
        if resource.get("module") or key not in ALLOWED or key in found:
            raise ValueError("unexpected managed resource")
        instances = resource.get("instances")
        if not isinstance(instances, list) or len(instances) > 1:
            raise ValueError("unexpected resource instances")
        if not instances:
            continue
        if instances[0].get("deposed") or instances[0].get("status") == "tainted":
            raise ValueError("resource state is ambiguous")
        attrs = instances[0].get("attributes") or {}
        resource_id = attrs.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            raise ValueError("resource id missing")
        found[key] = attrs
    if not found and expected_ids is None:
        raise ValueError("no state-managed resources")

    # During a retry, resources already removed from state are known from the
    # inventory captured before the first destroy attempt.
    previous = {}
    if expected_ids is not None:
        for entry in expected_ids:
            address, separator, resource_id = entry.partition(":")
            if not separator or not resource_id:
                raise ValueError("invalid original resource inventory")
            previous[address] = resource_id

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
    sg = (found.get(("huaweicloud_networking_secgroup", "app"), {}).get("id")
          or previous.get("huaweicloud_networking_secgroup.app"))
    for (rtype, _), attrs in found.items():
        if rtype == "huaweicloud_networking_secgroup_rule" and (not sg or attrs.get("security_group_id") != sg):
            raise ValueError("security group rule ownership is unclear")
    subnet = found.get(("huaweicloud_vpc_subnet", "app"))
    vpc = found.get(("huaweicloud_vpc", "app"), {}).get("id") or previous.get("huaweicloud_vpc.app")
    if subnet and (not vpc or subnet.get("vpc_id") != vpc):
        raise ValueError("subnet ownership is unclear")
    assoc = found.get(("huaweicloud_compute_eip_associate", "app"))
    instance = (found.get(("huaweicloud_compute_instance", "app"), {}).get("id")
                or previous.get("huaweicloud_compute_instance.app"))
    if assoc and (not instance or assoc.get("instance_id") != instance
                  or (not eip and not previous.get("huaweicloud_vpc_eip.app"))):
        raise ValueError("EIP association ownership is unclear")
    return sorted(f"{rtype}.{item}:{attrs['id']}" for (rtype, item), attrs in found.items())


def deploy(run_dir):
    checkout = upstream()
    data = manifest(run_dir)
    work = run_dir / "auto-deploy"
    if work.exists():
        raise ValueError("refusing to reuse deployment directory")
    region = os.environ["SMARTBOOK_CLOUD_REGION"]
    if not re.fullmatch(r"[a-z0-9-]{3,40}", region):
        raise ValueError("invalid cloud region")
    cidr = ipaddress.ip_network(os.environ["SMARTBOOK_CLOUD_CIDR"], strict=True)
    if cidr.version != 4 or cidr.prefixlen != 32:
        raise ValueError("control address must be an IPv4 /32")
    command = [sys.executable, str(checkout / "scripts" / "auto_deploy.py"),
               "--repo", str(run_dir / "staging"), "--app-name", data["app_name"],
               "--region", region, "--workdir", str(work), "--network-mode", "create",
               "--ssh-cidr", str(cidr), "--public-cidr", str(cidr), "--app-port", "5000",
               "--no-http", "--no-https", "--yes"]
    try:
        result = run_quiet(command, checkout)
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "ownership": "uncertain", "resource_ids": [],
                "error": "deployment_interrupted_inspect_retained_workdir",
                "diagnostic_directory": str(work)}
    try:
        ids = state_inventory(run_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "ownership": "uncertain", "resource_ids": [],
                "error": "ownership_unconfirmed_inspect_retained_workdir",
                "diagnostic_directory": str(work)}
    if result.returncode:
        return {"ok": False, "ownership": "owned", "resource_ids": ids,
                "error": "deployment_failed_cleanup_required", "diagnostic_directory": str(work)}
    try:
        info = read_json(work / "resource-info.json")
        host = str(ipaddress.IPv4Address(info["public_ip"]))
    except (OSError, KeyError, ValueError):
        return {"ok": False, "ownership": "owned", "resource_ids": ids,
                "error": "endpoint_missing_cleanup_required", "diagnostic_directory": str(work)}
    return {"ok": True, "ownership": "owned", "resource_ids": ids,
            "endpoint": f"http://{host}:5000/"}


def recover(run_dir):
    if os.environ.get("SMARTBOOK_RECOVERY_PROCESSES_STOPPED") != "yes":
        return {"ok": False, "ownership": "uncertain", "resource_ids": [],
                "error": "confirm_deploy_processes_stopped_before_recovery"}
    try:
        ids = state_inventory(run_dir)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "ownership": "uncertain", "resource_ids": [],
                "error": "ownership_unconfirmed_inspect_retained_workdir"}
    return {"ok": True, "ownership": "owned", "resource_ids": ids}

def verify(run_dir):
    data = manifest(run_dir)
    info = read_json(run_dir / "auto-deploy" / "resource-info.json")
    host = str(ipaddress.ip_address(info["public_ip"]))
    endpoint = f"http://{host}:5000/"
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(endpoint, timeout=20) as response:
            observed = response.read(2 * 1024 * 1024 + 1)
        expected = (run_dir / "staging" / "public" / "index.html").read_bytes()
        http_ok = observed == expected
    except (OSError, urllib.error.URLError):
        http_ok = False
    # A separate browser run must describe actual page interactions and console status.
    browser_path = Path(os.environ.get("SMARTBOOK_BROWSER_REPORT", str(run_dir / "browser-report.json")))
    browser = read_json(browser_path) if browser_path.exists() else {}
    checks = browser.get("checks", {}) if isinstance(browser.get("checks"), dict) else {}
    bound = browser.get("sha256") == data.get("sha256") and browser.get("endpoint") == endpoint
    output_checks = {key: bound and checks.get(key) is True for key in ("desktop", "mobile", "interaction", "console")}
    output_checks["http"] = http_ok
    return {"ok": all(output_checks.values()), "sha256": data.get("sha256"), "checks": output_checks}


def cleanup(run_dir):
    checkout = upstream()
    expected = read_json(run_dir / "pipeline.json").get("resource_ids")
    if (not isinstance(expected, list) or not expected
            or not all(isinstance(value, str) and value for value in expected)
            or len(set(expected)) != len(expected)):
        raise ValueError("original resource inventory is invalid")
    ids = state_inventory(run_dir, expected_ids=expected)
    if not set(ids).issubset(expected):
        raise ValueError("state inventory changed since deployment; refusing destroy")
    if not ids:
        return {"ok": True, "destroyed_ids": expected}
    result = run_quiet([sys.executable, str(checkout / "scripts" / "destroy.py"),
                        "--workdir", str(run_dir / "auto-deploy" / "terraform"), "--auto-approve"], checkout)
    if result.returncode:
        return {"ok": False, "destroyed_ids": [], "error": "destroy_failed_inspect_retained_workdir",
                "diagnostic_directory": str(run_dir / "auto-deploy")}
    return {"ok": True, "destroyed_ids": expected}


def confirm(run_dir):
    # Terraform state is useful cleanup evidence, not independent cloud verification.
    failed = {"ok": False, "absent_ids": [], "independently_verified": False,
              "error": "terraform_state_not_empty_or_unavailable"}
    try:
        state = read_json(run_dir / "auto-deploy" / "terraform" / "terraform.tfstate")
        resources = state["resources"]
        destroyed = read_json(run_dir / "pipeline.json")["destroyed_ids"]
        if not isinstance(resources, list) or not isinstance(destroyed, list) or not destroyed:
            return failed
        for resource in resources:
            if not isinstance(resource, dict) or not isinstance(resource.get("instances"), list):
                return failed
            if resource.get("mode") == "managed" and resource["instances"]:
                return failed
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return failed
    return {"ok": True, "absent_ids": destroyed, "verification": "terraform_state_empty",
            "independently_verified": False}


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("deploy", "recover", "verify", "cleanup", "confirm"):
        print(json.dumps({"ok": False, "error": "expected phase and run directory"}))
        return 1
    try:
        run_dir = Path(sys.argv[2]).resolve(strict=True)
        result = globals()[sys.argv[1]](run_dir)
    except DependencyError as exc:
        result = {"ok": False, "ownership": "uncertain", "resource_ids": [], "error": "dependency_setup_failed",
                  "message": str(exc)}
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError):
        result = {"ok": False, "ownership": "uncertain", "resource_ids": [],
                  "error": "adapter_failed_inspect_retained_workdir"}
    print(json.dumps(result, ensure_ascii=True))
    # Emit valid failure reports with success exit status so the orchestrator can
    # preserve an ownership result and attempt safe cleanup where possible.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
