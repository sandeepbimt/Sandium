#!/usr/bin/env python3
"""Offline tests for the fast APK cache hit gate."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("sandium_fast_apk", HERE / "fast_apk.py")
assert SPEC and SPEC.loader
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

PINS = {
    "bare_commit": "a" * 40, "chromium_commit": "b" * 40,
    "chromium_version": "156.0.8078.25", "target_abi": "arm64",
    "build_config": "Debug", "artifact_name": "Sandium-debug-arm64.apk",
}

class FastApkTests(unittest.TestCase):
    def fixture(self, root: Path):
        out = root / "sandium-output"
        stage = root / "ci-handoff" / "stage-1"
        out.mkdir(parents=True)
        stage.mkdir(parents=True)
        apk = out / "Sandium-debug-arm64.apk"
        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", "binary-manifest")
            archive.writestr("classes.dex", "dex")
        digest = hashlib.sha256(apk.read_bytes()).hexdigest()
        identity = "e" * 64
        manifest = {
            **PINS, "source": "Bare", "target": "android-arm64-debug-apk",
            "build_identity_sha256": identity, "artifact_sha256": digest,
            "artifact_bytes": apk.stat().st_size, "artifact_verified": True,
            "apksigner_verification": "PASS",
        }
        (out / "build-manifest.json").write_text(json.dumps(manifest))
        ledger = {"stage": 1, "build_identity_sha256": identity, "artifact_verified": True}
        (stage / "ledger.json").write_text(json.dumps(ledger))
        status = {
            "BUILD_FINISHED": "true", "APK_ARCHIVE_VALID": "true", "APK_SIGNATURE_VERIFIED": "true",
            "APK_SHA256": digest, "APK_SIZE_BYTES": str(apk.stat().st_size),
            "BUILD_IDENTITY_SHA256": identity, "STAGE": "1",
        }
        (stage / "status.txt").write_text("".join(f"{k}={v}\n" for k, v in status.items()))
        return apk, stage

    def test_valid_cached_apk_passes_saved_signature_record_and_hash_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            apk, stage = self.fixture(root)
            with patch.object(M, "read_pins", return_value=PINS):
                _, ledger, status = M.verify_apk(root, root / "control", stage)
            self.assertTrue(ledger["artifact_verified"])
            self.assertEqual(status["APK_SHA256"], hashlib.sha256(apk.read_bytes()).hexdigest())

    def test_modified_apk_fails_manifest_hash_check(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            apk, stage = self.fixture(root)
            apk.write_bytes(apk.read_bytes() + b"tamper")
            with patch.object(M, "read_pins", return_value=PINS):
                with self.assertRaisesRegex(ValueError, "SHA-256/size"):
                    M.verify_apk(root, root / "control", stage)

    def test_manifest_pin_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            apk, stage = self.fixture(root)
            manifest_path = root / "sandium-output" / "build-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["chromium_commit"] = "f" * 40
            manifest_path.write_text(json.dumps(manifest))
            with patch.object(M, "read_pins", return_value=PINS):
                with self.assertRaisesRegex(ValueError, "pin mismatch"):
                    M.verify_apk(root, root / "control", stage)

if __name__ == "__main__":
    unittest.main(verbosity=2)
