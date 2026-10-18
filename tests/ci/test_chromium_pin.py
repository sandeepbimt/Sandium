"""Offline tests for Sandium's manual Chromium pin and sync-cache fast path."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = ROOT / "ci/siso/chromium_pin.py"

spec = importlib.util.spec_from_file_location("chromium_pin", HELPER_PATH)
assert spec and spec.loader
chromium_pin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = chromium_pin
spec.loader.exec_module(chromium_pin)

PIN = "156.0.8078.39"


def run_git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


class ManualChromiumPinTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.core = self.workspace / "src" / "brave"
        (self.core / "ci/pins").mkdir(parents=True)
        (self.core / "ci/pins/chromium-tag.txt").write_text(
            "# explicit manual release pin\n" + PIN + "\n",
            encoding="utf-8",
        )
        (self.core / "package.json").write_text(
            json.dumps(
                {
                    "version": "1.100.10",
                    "config": {"projects": {"chrome": {"tag": "156.1.100.0"}}},
                }
            ),
            encoding="utf-8",
        )

        self.source = self.workspace / "src"
        (self.source / "chrome").mkdir(parents=True)
        self.version_file = self.source / "chrome/VERSION"
        self.version_file.write_text(
            "MAJOR=156\nMINOR=0\nBUILD=8078\nPATCH=39\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", str(self.source)], check=True, capture_output=True)
        run_git(self.source, "config", "user.email", "test@example.invalid")
        run_git(self.source, "config", "user.name", "Sandium offline test")
        run_git(self.source, "add", "chrome/VERSION")
        run_git(self.source, "commit", "-m", "test pinned source")
        run_git(self.source, "tag", PIN)

        self.gclient = self.workspace / ".gclient"
        self.gclient.write_text(
            "solutions = [{'name': 'src', 'url': 'https://example.invalid/chromium'}]\n",
            encoding="utf-8",
        )
        self.manifest_path = self.workspace / ".sandium-source-cache-manifest.json"
        self.manifest_path.write_text(
            json.dumps(
                {
                    "schema": 3,
                    "chromium_commit": run_git(self.source, "rev-parse", "HEAD"),
                    "chromium_version": PIN,
                    "chromium_tag": PIN,
                    "version_file_sha256": hashlib.sha256(
                        self.version_file.read_bytes()
                    ).hexdigest(),
                    "gclient_sha256": hashlib.sha256(self.gclient.read_bytes()).hexdigest(),
                    "target": "android-arm64-debug-apk",
                }
            ),
            encoding="utf-8",
        )
        self.sync_info_path = self.workspace / ".brave_latest_successful_sync.json"
        self.sync_info_path.write_text(
            json.dumps(
                {"chromiumRef": f"refs/tags/{PIN}", "gclientTimestamp": "old-file-mtime"}
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_compatible_cache_migrates_mtime_metadata_to_content_hash(self):
        reusable = chromium_pin.prepare_reuse(self.workspace, self.core)
        self.assertTrue(reusable)
        sync_info = json.loads(self.sync_info_path.read_text(encoding="utf-8"))
        self.assertEqual(sync_info["chromiumRef"], f"refs/tags/{PIN}")
        self.assertEqual(
            sync_info["gclientHash"],
            hashlib.sha256(self.gclient.read_bytes()).hexdigest(),
        )
        self.assertNotIn("gclientTimestamp", sync_info)

    def test_manual_pin_wins_over_newer_brave_package_declaration(self):
        self.assertNotEqual(
            json.loads((self.core / "package.json").read_text())["config"]["projects"]["chrome"]["tag"],
            PIN,
        )
        self.assertTrue(chromium_pin.prepare_reuse(self.workspace, self.core))

    def test_pin_change_does_not_normalize_old_source_cache(self):
        new_pin = "156.1.100.0"
        (self.core / "ci/pins/chromium-tag.txt").write_text(new_pin + "\n", encoding="utf-8")
        before = self.sync_info_path.read_text(encoding="utf-8")
        self.assertFalse(chromium_pin.prepare_reuse(self.workspace, self.core))
        self.assertEqual(self.sync_info_path.read_text(encoding="utf-8"), before)

    def test_invalid_manual_pin_is_rejected(self):
        (self.core / "ci/pins/chromium-tag.txt").write_text("latest\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            chromium_pin.read_manual_pin(self.core)

    def test_changed_gclient_contents_force_normal_sync(self):
        self.gclient.write_text("solutions = []\n", encoding="utf-8")
        before = self.sync_info_path.read_text(encoding="utf-8")
        self.assertFalse(chromium_pin.prepare_reuse(self.workspace, self.core))
        self.assertEqual(self.sync_info_path.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
