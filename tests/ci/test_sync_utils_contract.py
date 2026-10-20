"""Offline regression checks for the Chromium sync decision diagnostics.

These checks deliberately do not run gclient or contact any remote service.
"""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "build/commands/lib/syncUtils.ts"


class SyncDecisionDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE.read_text(encoding="utf-8")

    def test_sync_metadata_compares_known_fields_not_json_serialization(self):
        self.assertIn(
            "latestSyncInfo.chromiumRef !== expectedSyncInfo.chromiumRef",
            self.source,
        )
        self.assertIn(
            "latestSyncInfo.gclientHash !== expectedSyncInfo.gclientHash",
            self.source,
        )
        self.assertNotIn(
            "JSON.stringify(latestSyncInfo) !== JSON.stringify(expectedSyncInfo)",
            self.source,
        )

    def test_sync_decision_logs_both_gclient_hashes(self):
        self.assertIn("SANDIUM_SYNC_LATEST_GCLIENT_SHA256=", self.source)
        self.assertIn("SANDIUM_SYNC_EXPECTED_GCLIENT_SHA256=", self.source)
        self.assertIn("SANDIUM_SYNC_GCLIENT_HASH_MISMATCH=", self.source)

    def test_sync_decision_logs_revision_and_ref_reasons(self):
        self.assertIn("SANDIUM_SYNC_CURRENT_HEAD=", self.source)
        self.assertIn("SANDIUM_SYNC_TARGET_HEAD=", self.source)
        self.assertIn("SANDIUM_SYNC_REF_MISMATCH=", self.source)
        self.assertIn("SANDIUM_SYNC_REVISION_MISMATCH=", self.source)
        self.assertIn("SANDIUM_SYNC_DECISION=", self.source)

    def test_diagnostics_do_not_disable_required_sync(self):
        self.assertIn(
            "const needsUpdate = revisionMismatch || refMismatch || gclientHashMismatch",
            self.source,
        )
        self.assertIn("const shouldSyncChromium = chromiumNeedsUpdate || syncWithForce", self.source)
        self.assertIn("util.runGclient(args)", self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
