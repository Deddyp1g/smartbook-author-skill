import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "smartbook-author" / "scripts" / "cloud_pipeline.py"


class DeployExcludesTests(unittest.TestCase):
    def test_excluded_path_components_rejected_before_run_directory_created(self):
        for name in (".git", ".auto-deploy", "node_modules", ".venv", "venv", "target", "build", "dist", ".env"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "book.zip"
                with zipfile.ZipFile(archive, "w") as book:
                    book.writestr("index.html", "<html><body>Chapter</body></html>")
                    book.writestr(f"assets/{name}/image.png", b"image")
                run_dir = root / "run"
                result = subprocess.run([sys.executable, str(SCRIPT), "prepare", str(archive), "--run-dir", str(run_dir)], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0, name)
                self.assertIn("auto-deploy", result.stdout + result.stderr)
                self.assertFalse(run_dir.exists())

    def test_similar_names_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "book.zip"
            with zipfile.ZipFile(archive, "w") as book:
                book.writestr("index.html", "<html><body>Chapter</body></html>")
                book.writestr("assets/distribution/image.png", b"image")
            result = subprocess.run([sys.executable, str(SCRIPT), "prepare", str(archive), "--run-dir", str(root / "run")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
