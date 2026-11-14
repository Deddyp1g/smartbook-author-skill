import importlib.util
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "smartbook-author" / "scripts" / "smartbook.py"
spec = importlib.util.spec_from_file_location("smartbook", SCRIPT)
smartbook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smartbook)


class SmartbookPackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def archive(self, entries):
        target = self.root / "test.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with ZipFile(target, "w") as zf:
                for name, data in entries:
                    zf.writestr(name, data)
        return target

    def test_valid_book_allows_inline_script_and_local_storage(self):
        archive = self.archive([
            ("index.html", '<link href="./assets/book.css" rel="stylesheet"><script>localStorage.x=1</script><script src="assets/app.js"></script>'),
            ("assets/book.css", "body {}"), ("assets/app.js", "console.log('ok')"),
        ])
        self.assertTrue(smartbook.validate(archive)["ok"])

    def test_bad_paths_collisions_and_file_parent_conflicts_are_rejected(self):
        for bad in ("../outside.js", "/absolute.js", "C:/absolute.js"):
            with self.subTest(path=bad):
                result = smartbook.validate(self.archive([("index.html", "ok"), (bad, "bad")]))
                self.assertFalse(result["ok"], result)
        archive = self.archive([("index.html", "ok"), ("a/b.js", "bad")])
        archive.write_bytes(archive.read_bytes().replace(b"a/b.js", b"a\\b.js"))
        self.assertFalse(smartbook.validate(archive)["ok"])
        for names in (("Book.js", "book.js"), ("A.js", "a.js/x.js"), ("é.js", "e\u0301.js"), ("same.js", "same.js"), ("Dir/a.js", "dir/b.js")):
            with self.subTest(names=names):
                result = smartbook.validate(self.archive([("index.html", "ok"), (names[0], "a"), (names[1], "b")]))
                self.assertFalse(result["ok"], result)

    def test_missing_index_resource_nested_archive_and_symlink_are_rejected(self):
        link = ZipInfo("assets/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases = [
            [("chapter.html", "not the entry")],
            [("index.html", '<script src="missing.js"></script>')],
            [("index.html", "ok"), ("nested.zip", "fake")],
            [("index.html", "ok"), (link, "../elsewhere")],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                self.assertFalse(smartbook.validate(self.archive(entries))["ok"])

    def test_corrupt_archive_and_external_resource_warning(self):
        broken = self.root / "broken.zip"
        broken.write_bytes(b"not a zip")
        self.assertFalse(smartbook.validate(broken)["ok"])
        report = smartbook.validate(self.archive([("index.html", '<script src="https://example.com/app.js"></script>')]))
        self.assertTrue(report["ok"])
        self.assertTrue(report["warnings"])

    def test_encrypted_entry_flag_is_rejected(self):
        archive = self.archive([("index.html", "ok")])
        data = bytearray(archive.read_bytes())
        local = data.index(b"PK\x03\x04")
        central = data.index(b"PK\x01\x02")
        data[local + 6] |= 1
        data[central + 8] |= 1
        archive.write_bytes(data)
        result = smartbook.validate(archive)
        self.assertFalse(result["ok"], result)

    def test_platform_file_type_path_and_compression_rules(self):
        for name in ("bin/code.wasm", "bad.exe", "CON.js", "assets/NUL/image.png", "COM1.txt", "assets/space /x.js", "assets%2ffile.js"):
            with self.subTest(name=name):
                result = smartbook.validate(self.archive([("index.html", "ok"), (name, "content")]))
                self.assertFalse(result["ok"], result)
        for method in (ZIP_STORED, ZIP_DEFLATED, ZIP_BZIP2):
            with self.subTest(compression=method):
                archive = self.root / "compression.zip"
                with ZipFile(archive, "w", compression=method) as zf:
                    zf.writestr("index.html", "ok")
                result = smartbook.validate(archive)
                self.assertEqual(result["ok"], method != ZIP_BZIP2, result)

    def test_utf8_path_byte_limits(self):
        name = "中" * 85 + ".js"
        report = smartbook.validate(self.archive([("index.html", "ok"), (name, "ok")]))
        self.assertFalse(report["ok"], report)
        self.assertTrue(any("255 UTF-8 bytes" in err for err in report["errors"]))
        path = "/".join(["中" * 70] * 5 + ["book.js"])
        report = smartbook.validate(self.archive([("index.html", "ok"), (path, "ok")]))
        self.assertFalse(report["ok"], report)
        self.assertTrue(any("path must be relative" in err for err in report["errors"]))

    def test_high_compression_ratio_and_zero_compressed_size(self):
        archive = self.root / "ratio.zip"
        with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zf:
            zf.writestr("index.html", "A" * 100_000)
        report = smartbook.validate(archive)
        self.assertFalse(report["ok"], report)
        self.assertTrue(any("compression ratio" in err for err in report["errors"]))
        archive = self.archive([("index.html", "nonempty")])
        data = bytearray(archive.read_bytes())
        central = data.index(b"PK\x01\x02")
        data[central + 20:central + 24] = b"\x00\x00\x00\x00"
        archive.write_bytes(data)
        report = smartbook.validate(archive)
        self.assertFalse(report["ok"], report)
        self.assertTrue(any("compressed size is zero" in err for err in report["errors"]))

    def test_pack_output_and_limit_checks(self):
        source = self.root / "book"
        source.mkdir()
        (source / "index.html").write_text("hello", encoding="utf-8")
        (source / "app.js").write_text("hello", encoding="utf-8")
        target = self.root / "output.zip"
        with patch.object(smartbook, "MAX_FILES", 1):
            with self.assertRaises(ValueError):
                smartbook.pack(source, target)
        self.assertFalse(target.exists())
        self.assertTrue(smartbook.pack(source, target)["ok"])
        before = target.read_bytes()
        with self.assertRaises(ValueError):
            smartbook.pack(source, target)
        self.assertEqual(target.read_bytes(), before)
        with self.assertRaises(ValueError):
            smartbook.pack(source, source / "nested.zip")

    def test_cli_exit_status(self):
        source = self.root / "book"
        source.mkdir()
        (source / "index.html").write_text("hello", encoding="utf-8")
        target = self.root / "book.zip"
        result = subprocess.run([sys.executable, str(SCRIPT), "pack", str(source), str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run([sys.executable, str(SCRIPT), "validate", str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_supplied_example_when_available(self):
        example = Path("F:/大肥鱼/digital-textbook")
        if not example.is_dir():
            self.skipTest("example textbook not available")
        prepared = self.root / "example"
        prepared.mkdir()
        shutil.copy2(example / "index.html", prepared / "index.html")
        (prepared / "assets").mkdir()
        shutil.copytree(example / "assets" / "css", prepared / "assets" / "css")
        shutil.copytree(example / "assets" / "js", prepared / "assets" / "js")
        report = smartbook.pack(prepared, self.root / "example.zip")
        self.assertTrue(report["ok"], report)


if __name__ == "__main__":
    unittest.main()
