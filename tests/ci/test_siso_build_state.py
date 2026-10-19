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
        # use_remoteexec is a Brave CLI/runtime option; args.gn carries
        # only the actual GN switches for Siso and reclient.
        (self.out / "args.gn").write_text(
            "use_siso = true\nuse_reclient = false\n",
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

    def test_gn_fingerprint_accepts_brave_imported_generated_args(self):
        # The real Brave wrapper puts CLI --gn assignments in args_generated.gni
        # and args.gn imports it rather than repeating the assignments.
        (self.out / "args.gn").write_text(
            'import("//out/android_Debug_arm64/args_generated.gni")\\n',
            encoding="utf-8",
        )
        (self.out / "args_generated.gni").write_text(
            "use_siso = true\\nuse_reclient = false\\n",
            encoding="utf-8",
        )
        fingerprint = state_module._gn_fingerprint()
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")

    def test_gn_fingerprint_rejects_missing_siso_in_imported_generated_args(self):
        (self.out / "args.gn").write_text(
            'import("//out/android_Debug_arm64/args_generated.gni")\\n',
            encoding="utf-8",
        )
        (self.out / "args_generated.gni").write_text(
            "use_reclient = false\\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(state_module.StateError, "use_siso=true"):
            state_module._gn_fingerprint()

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

    def test_remote_metrics_parser_handles_siso_counters_split_across_lines(self):
        log = self.root / "split-siso-stats.log"
        log.write_text(
            "noexec:751 local:5637 remote:16659 cache:7 skip:19128\n"
            "cache-write:9(err:2) fallback:5 retry:4\n",
            encoding="utf-8",
        )
        metrics, found = state_module._remote_metrics(str(log))
        self.assertTrue(found)
        self.assertEqual(metrics["local_actions"], 5637)
        self.assertEqual(metrics["remote_actions"], 16659)
        self.assertEqual(metrics["remote_cache_hits"], 7)
        self.assertEqual(metrics["remote_cache_writes"], 9)
        self.assertEqual(metrics["remote_cache_write_errors"], 2)
        self.assertEqual(metrics["local_fallbacks"], 5)
        self.assertEqual(metrics["remote_retries"], 4)
        self.assertEqual(metrics["skipped_actions"], 19128)

    def test_remoteexec_cli_option_is_not_required_in_generated_gn_file(self):
        # Brave exposes --use_remoteexec as a build/runtime option, not a
        # required GN argument. The checkpoint must accept args.gn that has
        # valid Siso/reclient GN flags without a use_remoteexec line.
        digest = state_module._gn_fingerprint()
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_required_siso_gn_flags_are_checked_in_generated_file(self):
        (self.out / "args.gn").write_text(
            "use_siso = true\nuse_reclient = true\n",
            encoding="utf-8",
        )
        with self.assertRaises(state_module.StateError):
            state_module._gn_fingerprint()


if __name__ == "__main__":
    unittest.main(verbosity=2)
