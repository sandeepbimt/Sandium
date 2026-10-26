#!/usr/bin/env python3
"""Fast unit tests for source pin parsing and APK integrity verification."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("build_identity.py")
SPEC = importlib.util.spec_from_file_location("sandium_build_identity", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BuildIdentityTests(unittest.TestCase):
    def test_committed_source_pin_is_complete_and_pinned(self) -> None:
        values = MODULE.read_pins(Path(__file__).resolve().parents[2])
        self.assertEqual(values["bare_repository"], "https://github.com/BareBrowser/bare-browser.git")
        self.assertEqual(values["bare_commit"], "a5a7ef74f3b77d45720b8c502cbacc30f2f3dbd5")
        self.assertEqual(values["chromium_commit"], "1228231501697ff7a4a0150ea8ae6df37bb99331")
        self.assertEqual(values["chromium_version"], "156.0.8078.25")
        self.assertEqual(values["toolchain_depot_tools_commit"], "faceefec3fab70643e950caad40fded806877988")
        self.assertEqual(values["artifact_name"], "Sandium-debug-arm64.apk")

    def test_apk_integrity_requires_manifest_and_dex(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            apk = Path(temp) / "sample.apk"
            # Pad the archive beyond the minimum artifact size so the test checks
            # structure/CRC as well as the small-file rejection path.
            with zipfile.ZipFile(apk, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("AndroidManifest.xml", b"manifest-placeholder")
                archive.writestr("classes.dex", b"dex-placeholder")
                archive.writestr("assets/padding.bin", b"x" * 1_100_000)
            result = MODULE.verify_apk(apk, {"artifact_name": "Sandium-debug-arm64.apk"}, require_signature=False)
            self.assertFalse(result["artifact_verified"])
            self.assertEqual(result["apksigner_verification"], "NOT_REQUESTED")
            self.assertEqual(result["apk_zip_integrity"], "PASS")
            self.assertEqual(len(result["artifact_sha256"]), 64)

    def test_apk_without_dex_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            apk = Path(temp) / "bad.apk"
            with zipfile.ZipFile(apk, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("AndroidManifest.xml", b"manifest-placeholder")
                archive.writestr("assets/padding.bin", b"x" * 1_100_000)
            with self.assertRaises(MODULE.IdentityError):
                MODULE.verify_apk(apk, {"artifact_name": "Sandium-debug-arm64.apk"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
