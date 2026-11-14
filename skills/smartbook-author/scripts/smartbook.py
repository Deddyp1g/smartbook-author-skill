#!/usr/bin/env python3
"""Pack and perform preliminary, offline checks on a smartbook ZIP.

These checks are not a security review or a substitute for server validation.
"""

import argparse
import json
import os
import re
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import unicodedata
from html.parser import HTMLParser
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

MAX_ARCHIVE = 50 * 1024 * 1024
MAX_TOTAL = 16 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_FILES = 1000
MAX_HTML = 2 * 1024 * 1024
MAX_OUTPUT = 24 * 1024 * 1024
MAX_REFERENCES = 256
MAX_NODES = 20000
MAX_DEPTH = 128
MAX_CSS_COMMENTS = 20000
VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}
ALLOWED_EXTENSIONS = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json", ".png", ".jpg",
    ".jpeg", ".webp", ".gif", ".avif", ".ico", ".woff", ".woff2",
    ".ttf", ".otf", ".mp3", ".mp4", ".webm", ".wav", ".ogg", ".ogv",
    ".txt", ".md",
}
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
}


def path_error(name):
    if not name or len(name.encode("utf-8")) > 1024 or "\\" in name or "%" in name or "\x00" in name or name.startswith("/"):
        return "path must be relative and use forward slashes"
    parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "path contains an empty, dot, or parent component"
    if ":" in parts[0]:
        return "path contains a drive or scheme"
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "path contains a control character"
    for part in parts:
        if len(part.encode("utf-8")) > 255:
            return "path component exceeds 255 UTF-8 bytes"
        if part.rstrip(" .").split(".", 1)[0].upper() in WINDOWS_RESERVED:
            return "path contains a Windows reserved device name"
        if part.endswith((" ", ".")) or any(c in '<>:"|?*' for c in part):
            return "path contains a Windows-incompatible name"
    return None


def path_key(name):
    return unicodedata.normalize("NFC", name.rstrip("/")).casefold()


MEDIA_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico",
    ".mp3", ".mp4", ".ogg", ".ogv", ".webm", ".wav",
    ".woff", ".woff2", ".ttf", ".otf",
}


def resource_path(reference):
    value = reference.strip()
    if (not value or re.search(r"[?#%\\\x00-\x1f]", value) or value.startswith("/")
            or re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I)):
        return None
    path = value[2:] if value.startswith("./") else value
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


def css_error(value):
    if "\\" in value:
        return "CSS escapes/backslashes are unsupported"
    parts, position, comments = [], 0, 0
    while True:
        start = value.find("/*", position)
        if start < 0:
            parts.append(value[position:])
            break
        end = value.find("*/", start + 2)
        if end < 0:
            return "CSS has an unterminated comment"
        comments += 1
        if comments > MAX_CSS_COMMENTS:
            return "CSS exceeds the comment limit"
        parts.append(value[position:start])
        position = end + 2
    value = "".join(parts)
    if re.search(r"@import\b|\burl\s*\(", value, re.I):
        return "CSS url() and @import are unsupported"
    return None


class References(HTMLParser):
    def __init__(self, archive, files, errors, html_bytes):
        super().__init__(convert_charrefs=True)
        self.archive, self.files, self.errors = archive, files, errors
        self.style = None
        self.output_budget = html_bytes
        self.references = 0
        self.nodes = 3
        self.open_elements = []

    def count_node(self):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ValueError("reader HTML exceeds node limit")

    def charge(self, name):
        self.references += 1
        self.output_budget += 4 * ((self.files[name].file_size + 2) // 3) + 128
        if self.references > MAX_REFERENCES or self.output_budget > MAX_OUTPUT:
            raise ValueError("reader resource count or rendered size exceeds limit")

    def fail(self, message):
        self.errors.append(f"index.html: {message}")

    def file(self, ref, suffix):
        name = resource_path(ref)
        if not name or not name.lower().endswith(suffix):
            self.fail(f"invalid {suffix} resource {ref!r}")
            return None
        if name not in self.files:
            self.fail(f"missing resource {ref!r}")
            return None
        self.charge(name)
        return self.archive.read(self.files[name])

    def check_css(self, value):
        error = css_error(value)
        if error:
            self.fail(error)

    def handle_startendtag(self, tag, attrs):
        if tag in ("script", "style"):
            self.fail(f"self-closing <{tag}> is unsupported")
        super().handle_startendtag(tag, attrs)

    def handle_starttag(self, tag, attrs):
        self.count_node()
        if tag not in VOID_ELEMENTS:
            self.open_elements.append(tag)
            if len(self.open_elements) > MAX_DEPTH:
                raise ValueError("reader HTML exceeds depth limit")
        names = [name for name, _ in attrs]
        if len(names) != len(set(names)):
            self.fail(f"duplicate attribute on <{tag}>")
        attrs = dict(attrs)
        if tag in ("iframe", "frame", "object", "embed", "base", "form", "template"):
            self.fail(f"unsupported element <{tag}>")
        if tag == "meta" and "http-equiv" in attrs:
            self.fail("meta http-equiv is unsupported")
        if any(attr in attrs for attr in ("srcset", "formaction")):
            self.fail("srcset and formaction are unsupported")
        if attrs.get("style"):
            self.check_css(attrs["style"])
        if tag == "style":
            self.style = []
        if tag == "script":
            script_type = (attrs.get("type") or "").strip().lower()
            if script_type and script_type not in ("text/javascript", "application/javascript"):
                self.fail("only classic JavaScript scripts are supported")
            if "src" in attrs:
                self.file(attrs["src"] or "", ".js")
        elif tag == "link":
            rel = (attrs.get("rel") or "").lower().split()
            href = attrs.get("href")
            if href is None:
                self.fail("link requires href")
            elif "stylesheet" in rel:
                css = self.file(href, ".css")
                if css is not None:
                    try:
                        self.check_css(css.decode("utf-8-sig"))
                    except UnicodeDecodeError:
                        self.fail(f"stylesheet {href!r} must be UTF-8")
            elif "icon" not in rel or not href.startswith("data:image/"):
                self.fail("link must be a local stylesheet or data:image icon")
        else:
            if "src" in attrs:
                src = attrs["src"] or ""
                if tag not in ("img", "audio", "video", "source", "track"):
                    self.fail(f"unsupported src on <{tag}>")
                elif not src.startswith("data:"):
                    name = resource_path(src)
                    if not name or PurePosixPath(name).suffix.lower() not in MEDIA_EXTENSIONS or name not in self.files:
                        self.fail(f"missing or unsupported resource {src!r}")
                    else:
                        self.charge(name)
            if "href" in attrs and not (attrs["href"] or "").startswith("#"):
                self.fail("only same-document #anchor navigation is supported")
            if "poster" in attrs:
                self.fail("video poster is unsupported")

    def handle_data(self, data):
        if data:
            self.count_node()
        if self.style is not None:
            self.style.append(data)

    def handle_comment(self, data):
        self.count_node()

    def handle_decl(self, decl):
        self.count_node()

    def handle_endtag(self, tag):
        if tag in self.open_elements:
            last = len(self.open_elements) - 1 - self.open_elements[::-1].index(tag)
            del self.open_elements[last:]
        if tag == "style" and self.style is not None:
            self.check_css("".join(self.style))
            self.style = None


def validate(archive):
    errors, warnings = [], []
    archive = Path(archive)
    try:
        if archive.stat().st_size > MAX_ARCHIVE:
            errors.append("archive exceeds 50 MiB")
        with ZipFile(archive) as zf:
            entries = zf.infolist()
            files = [entry for entry in entries if not entry.is_dir()]
            if len(entries) > MAX_FILES:
                errors.append("archive contains more than 1000 entries")
            if sum(entry.file_size for entry in files) > MAX_TOTAL:
                errors.append("expanded files exceed 16 MiB")
            seen = set()
            spellings = {}
            safe_files = {}
            for entry in entries:
                # filename is normalized by zipfile on Windows; inspect the original.
                name = entry.orig_filename
                reason = path_error(name)
                if not entry.is_dir() and (resource_path(name) != name or name.strip() != name):
                    reason = "reader requires an unambiguous relative archive path"
                if reason:
                    errors.append(f"{name!r}: {reason}")
                    continue
                key = path_key(name)
                if key in seen:
                    errors.append(f"{name!r}: duplicate or case/Unicode-colliding path")
                seen.add(key)
                parts = name.rstrip("/").split("/")
                for i in range(1, len(parts) + 1):
                    spelling = tuple(parts[:i])
                    prefix_key = path_key("/".join(spelling))
                    if prefix_key in spellings and spellings[prefix_key] != spelling:
                        errors.append(f"{name!r}: directory spelling collision")
                    spellings[prefix_key] = spelling
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode) if entry.create_system == 3 else 0
                if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                    errors.append(f"{name!r}: symbolic link or special file is not allowed")
                if kind and (kind == stat.S_IFDIR) != entry.is_dir():
                    errors.append(f"{name!r}: ZIP entry type does not match path")
                if entry.flag_bits & (1 | 0x40 | 0x2000):
                    errors.append(f"{name!r}: encrypted entries are not allowed")
                if entry.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
                    errors.append(f"{name!r}: only stored and deflate compression are allowed")
                if entry.is_dir():
                    if entry.file_size:
                        errors.append(f"{name!r}: directory entry must be empty")
                    continue
                if entry.file_size > MAX_FILE:
                    errors.append(f"{name!r}: file exceeds 32 MiB")
                if entry.file_size and (not entry.compress_size or entry.file_size > 200 * entry.compress_size):
                    errors.append(f"{name!r}: compression ratio exceeds 200 or compressed size is zero")
                if PurePosixPath(name).suffix.lower() in ARCHIVE_EXTENSIONS:
                    errors.append(f"{name!r}: nested archive is not allowed")
                elif PurePosixPath(name).suffix.lower() not in ALLOWED_EXTENSIONS:
                    errors.append(f"{name!r}: file type is not allowed")
                safe_files[name] = entry
            if "index.html" not in safe_files:
                errors.append("root index.html is required")
            file_keys = {path_key(name) for name in safe_files}
            for name in safe_files:
                parts = name.split("/")
                for i in range(1, len(parts)):
                    if path_key("/".join(parts[:i])) in file_keys:
                        errors.append(f"{name!r}: a parent path is a file")
                        break
            # Stream all accepted entries to detect corrupt ZIP data without extracting.
            if not errors:
                actual_total = 0
                for name, entry in safe_files.items():
                    actual_file = 0
                    with zf.open(entry) as stream:
                        while True:
                            chunk = stream.read(1024 * 1024)
                            if not chunk:
                                break
                            actual_file += len(chunk)
                            actual_total += len(chunk)
                            if actual_file > MAX_FILE or actual_total > MAX_TOTAL:
                                raise ValueError("expanded data exceeds size limit")
                    if actual_file != entry.file_size:
                        errors.append(f"{name!r}: expanded size does not match ZIP directory")
            if not errors:
                try:
                    if safe_files["index.html"].file_size > MAX_HTML:
                        raise ValueError("index.html exceeds 2 MiB")
                    html = zf.read("index.html").decode("utf-8-sig")
                except UnicodeDecodeError:
                    errors.append("index.html must be UTF-8")
                else:
                    parser = References(zf, safe_files, errors, safe_files["index.html"].file_size)
                    parser.feed(html)
                    parser.close()
                    if parser.style is not None:
                        parser.check_css("".join(parser.style))
    except (OSError, BadZipFile, RuntimeError, ValueError, NotImplementedError) as exc:
        errors.append(f"cannot read archive: {exc}")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "scope": "ZIP structure and static index.html resource checks only; dynamic JavaScript is not inspected"}


def pack(source, output):
    source, output = Path(source).resolve(), Path(output).absolute()
    if not source.is_dir():
        raise ValueError("source must be a directory")
    if output.exists():
        raise ValueError("output already exists")
    if output.resolve().is_relative_to(source):
        raise ValueError("output must be outside the source directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".smartbook-", suffix=".zip", dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as zf:
            count, total = 0, 0
            for root, dirs, files in os.walk(source, followlinks=False):
                dirs.sort()
                files.sort()
                for filename in dirs + files:
                    path = Path(root) / filename
                    if path.is_symlink() or not (path.is_dir() or path.is_file()):
                        raise ValueError(f"symbolic link or special file is not allowed: {path}")
                for filename in files:
                    path = Path(root) / filename
                    name = path.relative_to(source).as_posix()
                    reason = path_error(name)
                    if reason:
                        raise ValueError(f"{name!r}: {reason}")
                    size = path.stat().st_size
                    count += 1
                    total += size
                    if size > MAX_FILE:
                        raise ValueError(f"{name!r}: file exceeds 32 MiB")
                    if count > MAX_FILES or total > MAX_TOTAL:
                        raise ValueError("source exceeds file count or total size limit")
                    zf.write(path, name)
        result = validate(temporary)
        if result["ok"]:
            # Do not overwrite a destination created since the initial check.
            created = False
            try:
                with temporary.open("rb") as src, output.open("xb") as dst:
                    created = True
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
            except OSError:
                if created:
                    output.unlink(missing_ok=True)
                raise
        return result
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pack_parser = sub.add_parser("pack", help="pack a prepared book directory")
    pack_parser.add_argument("source")
    pack_parser.add_argument("output")
    validate_parser = sub.add_parser("validate", help="check a ZIP without extracting")
    validate_parser.add_argument("archive")
    args = parser.parse_args()
    try:
        result = pack(args.source, args.output) if args.command == "pack" else validate(args.archive)
    except (OSError, ValueError) as exc:
        result = {"ok": False, "errors": [str(exc)], "warnings": []}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
