"""Exercise publication gates without real credentials or application services."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class BootstrapPreflightTests(unittest.TestCase):
    def test_effective_connection_boundaries(self):
        source = (ROOT / "scripts/bootstrap-local.sh").read_text()
        code = source.split("python -c '\n", 1)[1].split("\n'\n)", 1)[0]
        cases = [
            ({"DATABASE_URL": "postgresql://127.0.0.1/example"}, 0),
            ({"DATABASE_URL": "postgresql://[::1]/example"}, 0),
            ({"DATABASE_URL": "postgresql://localhost/example", "APP_ENV": "production"}, 1),
            ({"DATABASE_URL": "postgresql://example.invalid/example"}, 1),
            ({"DATABASE_URL": "postgresql://localhost/example?host=example.invalid"}, 1),
            ({"DATABASE_URL": "postgresql://localhost/example?hostaddr=192.0.2.1"}, 1),
            ({"DATABASE_URL": "postgresql://localhost/example", "PGHOSTADDR": "192.0.2.1"}, 1),
            ({"DATABASE_URL": "postgresql://localhost/example", "PGSERVICE": "external"}, 1),
        ]
        for settings, expected in cases:
            with self.subTest(settings=settings):
                environment = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
                environment.update({"APP_ENV": "local", **settings})
                result = subprocess.run(
                    [sys.executable, "-c", code], env=environment,
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)


@unittest.skipUnless(shutil.which("gitleaks"), "Gitleaks 8.30.1 is required")
class PublicationScannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        (self.repo / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/check-publication-secrets.sh", self.repo / "scripts")
        shutil.copy2(ROOT / ".gitleaks.toml", self.repo)
        self.git("init", "--quiet")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "--no-gpg-sign", "-m", "Synthetic scanner fixture")

    def git(self, *arguments):
        return subprocess.run(["git", *arguments], cwd=self.repo, check=True,
                              capture_output=True, text=True)

    def scan(self):
        return subprocess.run(["bash", "scripts/check-publication-secrets.sh"], cwd=self.repo,
                              capture_output=True, text=True)

    def test_clean_source_and_history_pass(self):
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_visible_private_environment_path_is_refused(self):
        (self.repo / ".env.local").write_text("SYNTHETIC_FIXTURE=not-a-secret\n")
        result = self.scan()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("non-example environment file", result.stderr)

    def test_credential_path_is_refused(self):
        (self.repo / "fixture.key").write_text("not-a-real-key\n")
        result = self.scan()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("credential/key path", result.stderr)

    def test_ignored_local_environment_does_not_enter_scan(self):
        (self.repo / ".gitignore").write_text(".env.local\n")
        (self.repo / ".env.local").write_text("SYNTHETIC_FIXTURE=not-a-secret\n")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_source_secret_is_detected_despite_inline_allow(self):
        # Construct a scanner canary, not a usable credential.
        canary = "ghp_" + hashlib.sha256(b"publication-source-canary").hexdigest()[:36]
        (self.repo / "example.py").write_text(f'token = "{canary}"  # gitleaks:allow\n')
        result = self.scan()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(canary, result.stdout + result.stderr)

    def test_staged_content_matching_worktree_passes(self):
        (self.repo / "example.py").write_text('message = "synthetic example"\n')
        self.git("add", "example.py")
        result = self.scan()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_staged_secret_missing_from_worktree_is_refused(self):
        self.assert_staged_secret_is_refused(remove=True)

    def test_staged_secret_replaced_in_worktree_is_refused(self):
        self.assert_staged_secret_is_refused(remove=False)

    def assert_staged_secret_is_refused(self, *, remove):
        canary = "ghp_" + hashlib.sha256(b"publication-index-canary").hexdigest()[:36]
        source = self.repo / "example.py"
        source.write_text(f'token = "{canary}"\n')
        self.git("add", "example.py")
        if remove:
            source.unlink()
        else:
            source.write_text('message = "synthetic clean working copy"\n')
        result = self.scan()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("staged content differs", result.stderr)
        self.assertNotIn(canary, result.stdout + result.stderr)

    def test_secret_on_another_branch_is_detected(self):
        canary = "ghp_" + hashlib.sha256(b"publication-history-canary").hexdigest()[:36]
        original = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.git("checkout", "--quiet", "-b", "synthetic-history")
        (self.repo / "example.py").write_text(f'token = "{canary}"\n')
        self.git("add", "example.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "--no-gpg-sign", "-m", "Synthetic history canary")
        self.git("checkout", "--quiet", original)
        result = self.scan()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(canary, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
