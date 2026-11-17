#!/usr/bin/env python3
"""Run desktop and mobile browser checks against a temporary smartbook preview."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.parse
from zipfile import ZipFile


def verify(run_dir, endpoint, scenario_path, browser_executable=None):
    run = Path(run_dir).resolve(strict=True)
    url = urllib.parse.urlsplit(endpoint)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        raise ValueError("endpoint must be an HTTP(S) origin without credentials, path, query or fragment")
    endpoint = endpoint.rstrip("/") + "/"
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    snapshot = (run / "snapshot.zip").read_bytes()
    sha = "sha256:" + hashlib.sha256(snapshot).hexdigest()
    if manifest.get("sha256") != sha:
        raise ValueError("snapshot does not match the prepared manifest")
    with ZipFile(run / "snapshot.zip") as archive:
        index_hash = hashlib.sha256(archive.read("index.html")).hexdigest()
    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
    steps = scenario.get("steps") if isinstance(scenario, dict) else None
    if not isinstance(steps, list) or not steps:
        raise ValueError("scenario must contain at least one click and visible-result assertion")
    for step in steps:
        if not isinstance(step, dict) or not all(isinstance(step.get(k), str) and step[k].strip() for k in ("name", "click", "expect_visible")):
            raise ValueError("each scenario step needs name, click and expect_visible selectors")

    command = ["node", str(Path(__file__).with_name("browser_verify.cjs")), endpoint, index_hash,
               json.dumps(steps, ensure_ascii=False)]
    if browser_executable:
        command.append(browser_executable)
    env = dict(os.environ)
    env.pop("SMARTBOOK_API_KEY", None)
    try:
        proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("browser runner could not start or timed out: " + str(exc)) from exc
    try:
        result = json.loads(proc.stdout)
    except ValueError as exc:
        raise ValueError("browser runner failed; install Node.js, playwright and a Chromium browser: " + proc.stderr[-1500:]) from exc
    report = {"sha256": sha, "endpoint": endpoint, **result}
    (run / "browser-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--browser-executable", help="optional Chromium-compatible browser executable")
    args = parser.parse_args()
    try:
        report = verify(args.run_dir, args.url, args.scenario, args.browser_executable)
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
