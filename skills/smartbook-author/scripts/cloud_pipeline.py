#!/usr/bin/env python3
"""Prepare a smartbook snapshot, run a temporary preview, and gate upload."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from zipfile import ZipFile

from upload import checked_snapshot, upload


HERE = Path(__file__).resolve().parent
REAL_ADAPTER = HERE / "huawei_cloud_adapter.py"
CHECKS = ("http", "desktop", "mobile", "interaction", "console")
PREVIEW_APP = (
    "from flask import Flask, send_from_directory\n"
    "app = Flask(__name__, static_folder=None)\n"
    "@app.get('/')\ndef index():\n    return send_from_directory('public', 'index.html')\n"
    "@app.get('/<path:name>')\ndef asset(name):\n    return send_from_directory('public', name)\n"
)
PREVIEW_REQUIREMENTS = "Flask==3.1.2\n"


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def load_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid pipeline state")
    return value


def prepare(archive, run_dir):
    data = checked_snapshot(archive)
    root = Path(run_dir).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    stage = root / "staging"
    public = stage / "public"
    public.mkdir(parents=True)
    snapshot = root / "snapshot.zip"
    snapshot.write_bytes(data)
    with ZipFile(snapshot) as zipped:
        for entry in zipped.infolist():
            if not entry.is_dir():
                target = public.joinpath(*entry.filename.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zipped.read(entry))
    # The external deployer recognizes a Flask application automatically.
    (stage / "app.py").write_text(PREVIEW_APP, encoding="utf-8")
    (stage / "requirements.txt").write_text(PREVIEW_REQUIREMENTS, encoding="utf-8")
    manifest = {"version": 1, "run_id": uuid.uuid4().hex, "sha256": digest(data),
                "app_name": "smartbook-" + uuid.uuid4().hex, "snapshot": "snapshot.zip"}
    write_json(root / "manifest.json", manifest)
    write_json(root / "pipeline.json", {"status": "prepared", "sha256": manifest["sha256"]})
    return {"ok": True, "run_dir": str(root), "sha256": manifest["sha256"], "status": "prepared"}


def call_adapter(adapter, phase, root, timeout):
    env = os.environ.copy()
    env.pop("SMARTBOOK_API_KEY", None)
    process = subprocess.run([sys.executable, str(adapter), phase, str(root)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout, env=env)
    try:
        value = json.loads(process.stdout)
    except ValueError as exc:
        raise ValueError(f"{phase} adapter returned invalid JSON; retain run directory") from exc
    if process.returncode != 0 or not isinstance(value, dict):
        raise ValueError(f"{phase} adapter failed; retain run directory")
    return value


def valid_ids(value):
    return (isinstance(value, list) and bool(value) and
            all(isinstance(item, str) and item and len(item) <= 512 for item in value) and
            len(value) == len(set(value)))


def check_staging(root):
    with ZipFile(root / "snapshot.zip") as archive:
        expected = {entry.filename: archive.read(entry) for entry in archive.infolist() if not entry.is_dir()}
    public = root / "staging" / "public"
    stage = public.parent
    if (set(path.name for path in stage.iterdir()) != {"app.py", "requirements.txt", "public"}
            or any(path.is_symlink() for path in stage.iterdir())
            or (stage / "app.py").read_text(encoding="utf-8") != PREVIEW_APP
            or (stage / "requirements.txt").read_text(encoding="utf-8") != PREVIEW_REQUIREMENTS):
        raise ValueError("preview staging contains unexpected or modified files")
    actual = {}
    for path in public.rglob("*"):
        if path.is_symlink():
            raise ValueError("staging contains a symbolic link")
        if path.is_file():
            actual[path.relative_to(public).as_posix()] = path.read_bytes()
    if actual != expected:
        raise ValueError("staged files do not match the validated snapshot")


def execute(run_dir, adapter=None, simulation=False, timeout=1800):
    if not simulation:
        raise ValueError("real cloud creation is disabled: provider-side deletion verification is not implemented")
    root = Path(run_dir).resolve(strict=True)
    manifest = load_json(root / "manifest.json")
    state = load_json(root / "pipeline.json")
    if state.get("status") != "prepared" or state.get("sha256") != manifest.get("sha256"):
        raise ValueError("run is not in prepared state")
    if digest(checked_snapshot(root / "snapshot.zip")) != manifest.get("sha256"):
        raise ValueError("snapshot changed after preparation")
    check_staging(root)
    if simulation:
        if adapter is None:
            raise ValueError("simulation requires an adapter")
        adapter = Path(adapter).resolve(strict=True)
    else:
        if adapter is not None and Path(adapter).resolve() != REAL_ADAPTER:
            raise ValueError("real runs require the bundled Huawei adapter")
        adapter = REAL_ADAPTER.resolve(strict=True)
    state = {"status": "deploying", "sha256": manifest["sha256"], "simulation": simulation}
    write_json(root / "pipeline.json", state)
    owned = False
    identifiers = []
    verified = False
    clean = False
    reason = "deployment failed"
    try:
        deployed = call_adapter(adapter, "deploy", root, timeout)
        identifiers = deployed.get("resource_ids")
        owned = deployed.get("ownership") == "owned" and valid_ids(identifiers)
        if not owned:
            raise ValueError("resource ownership is not established; do not destroy automatically")
        state.update(status="deployed", resource_ids=identifiers)
        write_json(root / "pipeline.json", state)
        if deployed.get("ok") is not True:
            raise ValueError("deployment failed")
        report = call_adapter(adapter, "verify", root, timeout)
        checks = report.get("checks")
        verified = (report.get("ok") is True and report.get("sha256") == manifest["sha256"]
                    and isinstance(checks, dict) and all(checks.get(key) is True for key in CHECKS))
        if not verified:
            raise ValueError("preview verification failed or does not match the snapshot")
        state["verification"] = report
        reason = "cleanup not confirmed"
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        reason = str(exc)
    finally:
        if owned:
            try:
                state["status"] = "cleaning"
                write_json(root / "pipeline.json", state)
                result = call_adapter(adapter, "cleanup", root, timeout)
                deleted = result.get("destroyed_ids")
                if result.get("ok") is not True or not valid_ids(deleted) or set(deleted) != set(identifiers):
                    raise ValueError("cleanup did not confirm every owned resource")
                confirmation = call_adapter(adapter, "confirm", root, timeout)
                absent = confirmation.get("absent_ids")
                clean = (confirmation.get("ok") is True and confirmation.get("independently_verified") is True
                         and valid_ids(absent) and set(absent) == set(identifiers))
                if not clean:
                    raise ValueError("independent cloud deletion verification was not completed")
                state["cleanup"] = {"destroyed_ids": deleted, "absent_ids": absent,
                                    "independently_verified": True}
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                reason = str(exc)
    state["status"] = "simulation_passed" if verified and clean and simulation else (
        "upload_ready" if verified and clean else "blocked")
    if state["status"] == "blocked":
        state["reason"] = reason
    write_json(root / "pipeline.json", state)
    return {"ok": state["status"] in ("simulation_passed", "upload_ready"),
            "status": state["status"], "sha256": manifest["sha256"],
            "reason": state.get("reason")}


def staged_start(run_dir, adapter, timeout=1800):
    """Simulation-only pause point for an agent to inspect a deployed preview."""
    root = Path(run_dir).resolve(strict=True)
    manifest = load_json(root / "manifest.json")
    state = load_json(root / "pipeline.json")
    if state.get("status") != "prepared" or state.get("sha256") != manifest.get("sha256"):
        raise ValueError("run is not prepared")
    if digest(checked_snapshot(root / "snapshot.zip")) != manifest["sha256"]:
        raise ValueError("snapshot changed after preparation")
    check_staging(root)
    adapter_path = Path(adapter).resolve(strict=True)
    state.update(status="deploying", simulation=True, adapter=str(adapter_path),
                 adapter_sha256=digest(adapter_path.read_bytes()))
    write_json(root / "pipeline.json", state)
    try:
        result = call_adapter(adapter_path, "deploy", root, timeout)
        ids = result.get("resource_ids")
        if result.get("ownership") != "owned" or not valid_ids(ids):
            state.update(status="blocked", reason="ownership uncertain; inspect run directory")
        else:
            state.update(status="awaiting_verification", resource_ids=ids, endpoint=result.get("endpoint"))
            if result.get("ok") is not True:
                state.update(status="verification_failed", reason="deployment failed; run staged-cleanup")
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        state.update(status="blocked", reason=str(exc))
    write_json(root / "pipeline.json", state)
    return {"ok": state["status"] == "awaiting_verification", "status": state["status"],
            "endpoint": state.get("endpoint"), "sha256": state["sha256"], "reason": state.get("reason")}


def staged_adapter(state):
    if state.get("simulation") is not True:
        raise ValueError("staged commands support simulation only")
    adapter = Path(state.get("adapter", "")).resolve(strict=True)
    if digest(adapter.read_bytes()) != state.get("adapter_sha256"):
        raise ValueError("adapter changed; refuse to continue")
    return adapter


def staged_verify(run_dir, timeout=1800):
    root = Path(run_dir).resolve(strict=True)
    state = load_json(root / "pipeline.json")
    if state.get("status") != "awaiting_verification":
        raise ValueError("run is not awaiting verification")
    adapter = staged_adapter(state)
    state["status"] = "verifying"
    write_json(root / "pipeline.json", state)
    try:
        if digest(checked_snapshot(root / "snapshot.zip")) != state["sha256"]:
            raise ValueError("snapshot changed after deployment")
        check_staging(root)
        report = call_adapter(adapter, "verify", root, timeout)
        checks = report.get("checks")
        if not (report.get("ok") is True and report.get("sha256") == state["sha256"]
                and isinstance(checks, dict) and all(checks.get(item) is True for item in CHECKS)):
            raise ValueError("preview verification failed or snapshot mismatch")
        state.update(status="verified", verification=report)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        state.update(status="verification_failed", reason=str(exc))
    write_json(root / "pipeline.json", state)
    return {"ok": state["status"] == "verified", "status": state["status"],
            "reason": state.get("reason"), "next": "staged-cleanup"}


def staged_cleanup(run_dir, timeout=1800):
    root = Path(run_dir).resolve(strict=True)
    state = load_json(root / "pipeline.json")
    if state.get("status") not in ("awaiting_verification", "verifying", "verified", "verification_failed", "cleaning", "cleanup_failed", "confirming", "confirmation_failed"):
        raise ValueError("run has no safely recoverable owned resources")
    ids = state.get("resource_ids")
    if not valid_ids(ids):
        raise ValueError("resource ownership is not established; inspect the run directory")
    adapter = staged_adapter(state)
    try:
        if state.get("status") in ("confirming", "confirmation_failed"):
            deleted = state.get("destroyed_ids")
            if not valid_ids(deleted) or set(deleted) != set(ids):
                raise ValueError("cleanup evidence is missing")
        else:
            state["status"] = "cleaning"
            write_json(root / "pipeline.json", state)
            result = call_adapter(adapter, "cleanup", root, timeout)
            deleted = result.get("destroyed_ids")
            if result.get("ok") is not True or not valid_ids(deleted) or set(deleted) != set(ids):
                raise ValueError("cleanup did not confirm every owned resource")
            state.update(status="confirming", destroyed_ids=deleted)
            write_json(root / "pipeline.json", state)
        result = call_adapter(adapter, "confirm", root, timeout)
        absent = result.get("absent_ids")
        if not (result.get("ok") is True and result.get("independently_verified") is True
                and valid_ids(absent) and set(absent) == set(ids)):
            raise ValueError("independent cloud deletion verification was not completed")
        state["cleanup"] = {"destroyed_ids": deleted, "absent_ids": absent, "independently_verified": True}
        state["status"] = "simulation_passed" if state.get("verification", {}).get("ok") is True else "simulation_cleaned"
        state.pop("reason", None)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        state.update(status="confirmation_failed" if state.get("status") == "confirming" else "cleanup_failed", reason=str(exc))
    write_json(root / "pipeline.json", state)
    return {"ok": state["status"] in ("simulation_passed", "simulation_cleaned"),
            "status": state["status"], "reason": state.get("reason")}


def upload_ready(run_dir, book_id, base_url, idempotency_key=None):
    root = Path(run_dir).resolve(strict=True)
    manifest = load_json(root / "manifest.json")
    state = load_json(root / "pipeline.json")
    if state.get("status") != "upload_ready" or state.get("simulation") is not False:
        raise ValueError("upload blocked: real verification and independent cleanup are required")
    data = checked_snapshot(root / "snapshot.zip")
    sha = digest(data)
    report = state.get("verification", {})
    checks = report.get("checks", {})
    clean = state.get("cleanup", {})
    ids = state.get("resource_ids")
    if (sha != manifest.get("sha256") or sha != state.get("sha256")
            or report.get("sha256") != sha or report.get("ok") is not True
            or not all(checks.get(key) is True for key in CHECKS)
            or clean.get("independently_verified") is not True or not valid_ids(ids)
            or set(clean.get("destroyed_ids", [])) != set(ids) or set(clean.get("absent_ids", [])) != set(ids)):
        raise ValueError("upload blocked: snapshot or verification evidence does not match")
    previous_key = state.get("upload_idempotency_key")
    if previous_key and idempotency_key and idempotency_key != previous_key:
        raise ValueError("upload blocked: retry must reuse the prior idempotency key")
    previous_target = state.get("upload_target")
    target = {"book_id": book_id, "base_url": base_url}
    if previous_target and previous_target != target:
        raise ValueError("upload blocked: retry must use the prior book and origin")
    key = previous_key or idempotency_key or str(uuid.uuid4())
    # Persist before network I/O: the same key is needed after an unknown outcome.
    state["upload_idempotency_key"] = key
    state["upload_target"] = target
    write_json(root / "pipeline.json", state)
    with tempfile.TemporaryDirectory(prefix="smartbook-cloud-upload-") as directory:
        snapshot = Path(directory) / "snapshot.zip"
        snapshot.write_bytes(data)
        result = upload(snapshot, book_id, base_url, os.environ.get("SMARTBOOK_API_KEY", ""), key)
    if result.get("ok"):
        state["status"] = "uploaded"
        state["upload_result"] = result
        write_json(root / "pipeline.json", state)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("archive")
    prep.add_argument("--run-dir", required=True)
    run = sub.add_parser("execute")
    run.add_argument("--run-dir", required=True)
    run.add_argument("--adapter")
    run.add_argument("--simulation", action="store_true")
    run.add_argument("--timeout", type=int, default=1800)
    start = sub.add_parser("staged-start", help="simulation only: pause after deployment")
    start.add_argument("--run-dir", required=True)
    start.add_argument("--adapter", required=True)
    start.add_argument("--timeout", type=int, default=1800)
    for name in ("staged-verify", "staged-cleanup"):
        stage = sub.add_parser(name, help="simulation only: verify or recover cleanup")
        stage.add_argument("--run-dir", required=True)
        stage.add_argument("--timeout", type=int, default=1800)
    send = sub.add_parser("upload")
    send.add_argument("--run-dir", required=True)
    send.add_argument("--book-id", type=int, required=True)
    send.add_argument("--base-url", required=True)
    send.add_argument("--idempotency-key")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.archive, args.run_dir)
        elif args.command == "execute":
            result = execute(args.run_dir, args.adapter, args.simulation, args.timeout)
        elif args.command == "staged-start":
            result = staged_start(args.run_dir, args.adapter, args.timeout)
        elif args.command == "staged-verify":
            result = staged_verify(args.run_dir, args.timeout)
        elif args.command == "staged-cleanup":
            result = staged_cleanup(args.run_dir, args.timeout)
        else:
            result = upload_ready(args.run_dir, args.book_id, args.base_url, args.idempotency_key)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
