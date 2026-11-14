#!/usr/bin/env python3
"""Validate and upload a smartbook to an existing textbook."""

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

from smartbook import MAX_ARCHIVE, validate

SAFE_ERROR_CODES = {
    "archive_compression_ratio_exceeded", "archive_file_too_large", "archive_path_collision",
    "archive_too_large", "book_media_quota_exceeded", "encrypted_archive", "idempotency_conflict",
    "invalid_archive", "invalid_archive_count", "invalid_archive_file_type", "invalid_archive_path",
    "invalid_idempotency_key", "invalid_media", "invalid_media_kind", "invalid_request", "missing_index",
    "not_found", "payload_too_large", "resource_forbidden", "source_unavailable", "unauthorized",
    "unsupported_asset_type", "unsupported_compression", "unsupported_media_type",
    "unsupported_reader_complexity", "unsupported_reader_css", "unsupported_reader_element",
    "unsupported_reader_navigation", "unsupported_reader_resource", "unsupported_reader_script",
    "unsupported_reader_utf8",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def is_loopback(host):
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def origin_url(value):
    try:
        url = urllib.parse.urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                or url.password is not None or url.path not in ("", "/") or url.query or url.fragment
                or any(ord(c) < 33 or ord(c) == 127 for c in value)):
            raise ValueError
        port = url.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        return value.rstrip("/")
    except ValueError as exc:
        raise ValueError("base URL must be an HTTP(S) origin without credentials, path, query or fragment") from None


def checked_snapshot(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_ARCHIVE + 1)
    if not data or len(data) > MAX_ARCHIVE:
        raise ValueError("ZIP is empty or exceeds the archive size limit")
    # Validate the exact bytes sent, even if the original file changes.
    with tempfile.TemporaryDirectory(prefix="smartbook-upload-") as directory:
        snapshot = Path(directory) / "snapshot.zip"
        snapshot.write_bytes(data)
        result = validate(snapshot)
    if not result["ok"]:
        raise ValueError("ZIP validation failed; run smartbook.py validate for details")
    return data


def upload(archive, book_id, base_url, api_key, idempotency_key, timeout=120):
    if type(book_id) is not int or not 1 <= book_id <= 2147483647:
        raise ValueError("book ID must be a positive integer no greater than 2147483647")
    origin = origin_url(base_url)
    if not isinstance(api_key, str) or not api_key.startswith("stpk_") or not re.fullmatch(r"[\x21-\x7e]{6,512}", api_key):
        raise ValueError("set SMARTBOOK_API_KEY to a valid author API key")
    if not isinstance(idempotency_key, str) or not re.fullmatch(r"[\x21-\x7e]{1,200}", idempotency_key):
        raise ValueError("idempotency key must contain 1-200 printable ASCII characters without spaces")
    data = checked_snapshot(archive)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    request = urllib.request.Request(
        f"{origin}/v1/author/books/{book_id}/media/smartbook", data=data, method="POST",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/zip",
                 "Accept": "application/json", "Idempotency-Key": idempotency_key},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            body = response.read(65537)
    except urllib.error.HTTPError as exc:
        try:
            result = {"ok": False, "http_status": exc.code, "error": "server_rejected_upload",
                      "idempotency_key": idempotency_key, "retry": "If the outcome is uncertain, retry the same archive with this same idempotency key."}
            try:
                body = exc.read(65537)
                details = json.loads(body) if len(body) <= 65536 else None
                error = details.get("error") if isinstance(details, dict) else None
                code = error.get("code") if isinstance(error, dict) else None
                if isinstance(code, str) and code in SAFE_ERROR_CODES:
                    result["server_code"] = code
            except (ValueError, UnicodeDecodeError, OSError, http.client.HTTPException):
                pass
            return result
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        return {"ok": False, "error": "upload_outcome_unknown", "idempotency_key": idempotency_key,
                "retry": "Retry the same archive with this same idempotency key; do not assume the upload failed."}
    try:
        value = json.loads(body) if len(body) <= 65536 else None
        if (status != 201 or not isinstance(value, dict) or value.get("kind") != "smartbook"
                or value.get("sha256") != digest or value.get("size_bytes") != len(data)
                or value.get("status") not in ("pending_review", "approved", "rejected", "reviewed", "review_error")
                or type(value.get("published")) is not bool):
            raise ValueError
        asset_id = str(uuid.UUID(value["id"]))
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeDecodeError):
        return {"ok": False, "error": "unverified_server_response", "idempotency_key": idempotency_key,
                "retry": "Check the author workspace or retry the same archive with this same idempotency key."}
    return {"ok": True, "book_id": book_id, "asset_id": asset_id, "sha256": digest,
            "size_bytes": len(data), "status": value["status"], "published": value["published"],
            "idempotency_key": idempotency_key}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", help="validated smartbook ZIP")
    parser.add_argument("--book-id", required=True, type=int, help="existing textbook ID")
    parser.add_argument("--base-url", required=True, help="platform origin, for example https://example.com")
    parser.add_argument("--idempotency-key", help="reuse the key from an earlier uncertain upload")
    args = parser.parse_args()
    key = args.idempotency_key or str(uuid.uuid4())
    secret = os.environ.get("SMARTBOOK_API_KEY", "")
    if not re.fullmatch(r"[\x21-\x7e]{1,200}", key) or (secret and secret in key):
        print(json.dumps({"ok": False, "error": "invalid_idempotency_key"}), file=sys.stderr)
        return 1
    # Emit before network IO so a process interruption does not lose the key.
    print("Idempotency-Key: " + key, file=sys.stderr, flush=True)
    try:
        parsed = urllib.parse.urlsplit(args.base_url)
        if parsed.scheme == "http" and parsed.hostname and not is_loopback(parsed.hostname):
            print("Warning: HTTP exposes the API key and book to the network.", file=sys.stderr)
        result = upload(args.archive, args.book_id, args.base_url, secret, key)
    except (ValueError, OSError) as exc:
        message = str(exc)
        result = {"ok": False, "error": message.replace(secret, "[redacted]") if secret else message,
                  "idempotency_key": key}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
