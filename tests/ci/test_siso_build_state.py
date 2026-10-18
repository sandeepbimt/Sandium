"""Unit tests for SANDIUM's resumable build-state validator."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import hashlib
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ci/siso/build_state.py"
spec = importlib.util.spec_from_file_location("sandium_build_state", MODULE_PATH)
assert spec and spec.loader
state_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = state_module
spec.loader.exec_module(state_module)


class BuildStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.root = self.project / "src" / "brave-core"
        self.chromium = self.root / "src"
        self.out = self.chromium / "out" / "android_Debug_arm64"
        (self.chromium / "chrome").mkdir(parents=True)
        (self.root / "package.json").write_text(
            json.dumps({"config": {"projects": {"chrome": {"tag": "156.0.8078.39"}}}}),
            encoding="utf-8",
        )
        (self.chromium / "chrome" / "VERSION").write_text(
            "MAJOR=1\nMINOR=100\nBUILD=10\nPATCH=0\n",
            encoding="utf-8",
        )
        self._git_init(self.root)
        self._git_init(self.chromium)
        subprocess.run(["git", "-C", str(self.chromium), "tag", "156.1.100.0"], check=True)
        self.out.mkdir(parents=True)
        (self.out / "args.gn").write_text(
            "use_remoteexec = true\nuse_siso = true\nuse_reclient = false\n",
            encoding="utf-8",
        )
        (self.out / "apks").mkdir()
        self.apk = self.out / "apks" / "Bravearm64.apk"
        self.apk.write_bytes(b"A" * 1_100_000)
        self.state_file = self.out / ".sandium-build-state.json"
        self.patches = [
            patch.object(state_module, "CORE_DIR", self.root),
            patch.object(state_module, "CHROMIUM_DIR", self.chromium),
            patch.object(state_module, "OUTPUT_DIR", self.out),
            patch.object(state_module, "STATE_FILE", self.state_file),
            patch.object(state_module, "APK_FILE", self.apk),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self._stop_patches)
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True
        ).strip()

    def _git_init(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "SANDIUM test"], check=True)
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-qm", "test fixture"], check=True)

    def _stop_patches(self):
        for item in reversed(self.patches):
            item.stop()

    def _record(self, complete="true", verified="false", log=None):
        return state_module.command_record(
            Namespace(
                commit=self.commit,
                stage="stage-4-finish-and-package",
                compile_complete=complete,
                artifact_verified=verified,
                log=log,
            )
        )

    def test_chromium_identity_uses_synced_git_tag_not_declared_package_tag_or_VERSION(self):
        self.assertEqual(state_module._chromium_version(), "156.1.100.0")

    def test_compiled_state_requires_exact_source_and_apk_hash(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self._record(), 0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                state_module.command_check(
                    Namespace(mode="compiled", commit=self.commit)
                ),
                0,
            )
        self.apk.write_bytes(b"B" * 1_100_000)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                state_module.command_check(
                    Namespace(mode="compiled", commit=self.commit)
                ),
                1,
            )

    def test_verified_state_requires_real_siso_remote_or_cache_evidence(self):
        log = self.root / "siso.log"
        log.write_text(
            "local:12 remote:4 cache:7 cache-write:2(err:0) fallback:0 retry:0 skip:3\n",
            encoding="utf-8",
        )
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self._record(verified="true", log=str(log)), 0)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertTrue(state["artifact_verified"])
        self.assertTrue(state["remote_backend_proven"])
        self.assertEqual(state["remote_metrics"]["remote_actions"], 4)
        self.assertEqual(state["remote_metrics"]["remote_cache_hits"], 7)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                state_module.command_check(
                    Namespace(mode="verified", commit=self.commit)
                ),
                0,
            )

    def test_apk_verification_is_not_blocked_when_remote_stats_are_missing(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self._record(verified="true"), 0)
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertTrue(state["artifact_verified"])
        self.assertFalse(state["remote_backend_proven"])

    def test_artifact_cache_reuses_only_a_verified_matching_apk(self):
        output = self.project / "sandium-output"
        output.mkdir(parents=True)
        apk = output / "Brave-debug-arm64.apk"
        with zipfile.ZipFile(apk, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("AndroidManifest.xml", b"manifest")
            archive.writestr("classes.dex", b"D" * 1_100_000)

        digest = hashlib.sha256(apk.read_bytes()).hexdigest()
        size = apk.stat().st_size
        fingerprint = state_module._build_input_fingerprint()
        state = {
            "schema": 1,
            "target": state_module.TARGET,
            "build_config": "Debug",
            "chromium_version": "156.1.100.0",
            "build_input_fingerprint": fingerprint,
            "compile_complete": True,
            "artifact_verified": True,
            "apk_sha256": digest,
            "apk_bytes": size,
        }
        manifest = {
            "chromium_version": "156.1.100.0",
            "apk_sha256": digest,
            "apk_bytes": size,
            "artifact_verified": True,
            "apk_zip_integrity": "PASS",
            "dalvik_bytecode_present": True,
            "apksigner_verification": "PASS",
            "build_input_fingerprint": fingerprint,
            "app_package_name": "com.brave.browser",
        }
        (output / "build-state.json").write_text(json.dumps(state), encoding="utf-8")
        (output / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (output / "apk-badging.txt").write_text(
            "package: name='com.brave.browser' versionCode='1' versionName='1.0'\n",
            encoding="utf-8",
        )
        (output / "apksigner-verify.txt").write_text("Verified using v2 scheme: true\n", encoding="utf-8")

        cached = state_module._valid_artifact_cache(self.commit)
        self.assertTrue(cached["artifact_verified"])

        apk.write_bytes(apk.read_bytes() + b"tamper")
        with self.assertRaises(state_module.StateError):
            state_module._valid_artifact_cache(self.commit)

    def test_required_gn_flags_are_checked_in_generated_file(self):
        (self.out / "args.gn").write_text(
            "use_remoteexec = true\nuse_siso = true\nuse_reclient = true\n",
            encoding="utf-8",
        )
        with self.assertRaises(state_module.StateError):
            state_module._gn_fingerprint()


if __name__ == "__main__":
    unittest.main(verbosity=2)
