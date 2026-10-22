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

    def test_preflight_only_status_is_allowed_only_when_compile_windows_are_disabled(self):
        for stage in (1, 2):
            with self.subTest(stage=stage):
                segment = job_segment(
                    f"stage-{stage}-" + (
                        "source-foundation" if stage == 1 else "graph-and-compile-window"
                    ),
                    "stage-2-graph-and-compile-window" if stage == 1 else "stage-3-compile-continuation",
                )
                self.assertIn('[[ "$status" == "125" &&', segment)
                self.assertIn('<< pipeline.parameters.run_sandium_compile_windows >>" != "true"', segment)
                self.assertIn(f"BUILD_STAGE_PREFLIGHT_ONLY_HANDOVER_ALLOWED=true stage={stage} status=125", segment)
                self.assertLess(
                    segment.index("Save stage-" + str(stage) + " resumable build checkpoint"),
                    segment.index("Propagate compile failure only after saving resumable state"),
                )

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
                    segment.index("- checkout:\n          path: brave-core"),
                    segment.index("- snapshot-sandium-source-cache-inputs"),
                )
                self.assertLess(
                    segment.index("- snapshot-sandium-source-cache-inputs"),
                    segment.index("- restore-sandium-source"),
                )
                self.assertLess(
                    segment.index("Place Brave Core checkout after restoring Chromium source"),
                    segment.index("- validate-sandium-source-cache-inputs"),
                )

    def test_full_build_reserves_about_30_minutes_for_each_compile_window(self):
        # Source restore and GN preparation happen before Stage 1 compilation.
        # Keep a bounded 30-minute compile window and a five-minute handover
        # reserve within a 55-minute stage budget.
        self.assertEqual(CONFIG.count("--max-stage-seconds 3300"), 5)
        self.assertEqual(CONFIG.count("--handover-reserve-seconds 300"), 5)
        self.assertEqual(CONFIG.count("--max-compile-seconds 1800"), 5)
        self.assertEqual(CONFIG.count("3300 - elapsed_seconds - 300"), 5)
        self.assertEqual(CONFIG.count("-gt 1800"), 5)
        self.assertNotIn("--max-stage-seconds 3000", CONFIG)
        self.assertNotIn("--max-stage-seconds 3900", CONFIG)
        self.assertNotIn("--handover-reserve-seconds 600", CONFIG)
        self.assertNotIn("--max-compile-seconds 2400", CONFIG)
    def test_cache_manifest_binds_chromium_cache_to_brave_sync_inputs(self):
        self.assertIn("brave_sync_inputs_fingerprint", CONFIG)
        self.assertIn("source_cache_key.py", CONFIG)
        self.assertIn("SANDIUM_SOURCE_CACHE_INPUT_FINGERPRINT=PASS", CONFIG)

    def test_remote_preflight_script_path_is_inside_checked_out_brave_core(self):
        self.assertIn('PREFLIGHT="$HOME/project/src/brave/ci/siso/remote_preflight.py"', CONFIG)

    def test_source_cache_key_is_stable_across_ci_only_commits_and_branches(self):
        self.assertIn("sandium-src-v7-", CONFIG)
        self.assertIn('.sandium-source-cache-inputs/ci/pins/chromium-tag.txt', CONFIG)
        self.assertIn('.sandium-source-cache-inputs/build/commands/lib/syncUtils.ts', CONFIG)
        self.assertIn("Snapshot stable source-cache identity files before restoring Chromium", CONFIG)
        self.assertIn("Save Chromium source for reuse across branches with identical sync inputs", CONFIG)

    def test_siso_action_cache_is_shared_across_branches(self):
        self.assertIn("sandium-siso-v4-android-debug-arm64-", CONFIG)
        for stage in range(1, 6):
            self.assertIn(f"key: sandium-siso-v4-android-debug-arm64-stage{stage}-", CONFIG)


if __name__ == "__main__":
    unittest.main()
