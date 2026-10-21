"""Static regression checks for the resumable Android APK pipeline contract."""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = (ROOT / ".circleci/config.yml").read_text(encoding="utf-8")


def job_segment(name: str, following: str) -> str:
    start = CONFIG.index(f"  {name}:\n")
    end = CONFIG.index(f"  {following}:\n", start)
    return CONFIG[start:end]


class ResumablePipelineContractTests(unittest.TestCase):
    def test_all_five_production_stages_are_serialized(self):
        names = [
            "stage-1-source-foundation",
            "stage-2-graph-and-compile-window",
            "stage-3-compile-continuation",
            "stage-4-finish-and-package",
            "stage-5-verify-publish",
        ]
        for name in names:
            self.assertIn(f"  {name}:\n", CONFIG)
        workflow = CONFIG[CONFIG.index("  sandium-android-debug-apk:"):CONFIG.index("  sandium-ci-smoke-test:")]
        self.assertIn("requires:", workflow)
        self.assertIn("stage-5-verify-publish", workflow)

    def test_compile_failures_are_deferred_until_checkpoint_save(self):
        stages = [
            ("stage-1-source-foundation", "stage-2-graph-and-compile-window", "compile-window-1-exit.txt"),
            ("stage-2-graph-and-compile-window", "stage-3-compile-continuation", "compile-window-2-exit.txt"),
            ("stage-3-compile-continuation", "stage-4-finish-and-package", "compile-window-3-exit.txt"),
            ("stage-4-finish-and-package", "stage-5-verify-publish", "compile-window-4-exit.txt"),
        ]
        for current, following, status_file in stages:
            with self.subTest(stage=current):
                segment = job_segment(current, following)
                self.assertIn("COMPILE_WINDOW_FAILURE_DEFERRED_FOR_CHECKPOINT", segment)
                self.assertIn(status_file, segment)
                checkpoint = segment.index("Save stage-" + current.split("-")[1] + " resumable build checkpoint")
                failure_gate = segment.index("Propagate compile failure only after saving resumable state")
                self.assertLess(checkpoint, failure_gate)

    def test_continuation_stages_validate_source_identity_after_checkout(self):
        for current, following in [
            ("stage-2-graph-and-compile-window", "stage-3-compile-continuation"),
            ("stage-3-compile-continuation", "stage-4-finish-and-package"),
            ("stage-4-finish-and-package", "stage-5-verify-publish"),
            ("stage-5-verify-publish", "smoke-1-contract-and-foundation"),
        ]:
            with self.subTest(stage=current):
                segment = job_segment(current, following)
                self.assertLess(
                    segment.index("- checkout:\n          path: src/brave"),
                    segment.index("- validate-sandium-source-cache-inputs"),
                )

    def test_cache_manifest_binds_chromium_cache_to_brave_sync_inputs(self):
        self.assertIn("brave_sync_inputs_fingerprint", CONFIG)
        self.assertIn("source_cache_key.py", CONFIG)
        self.assertIn("SANDIUM_SOURCE_CACHE_INPUT_FINGERPRINT=PASS", CONFIG)

    def test_remote_preflight_script_path_is_inside_checked_out_brave_core(self):
        self.assertIn('PREFLIGHT="$HOME/project/src/brave/ci/siso/remote_preflight.py"', CONFIG)


if __name__ == "__main__":
    unittest.main()
