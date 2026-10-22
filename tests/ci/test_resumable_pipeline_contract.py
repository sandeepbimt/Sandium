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
    def test_duplicate_fast_regression_workflow_is_opt_in(self):
        # A production full build runs this gate inside Stage 1 before Chromium
        # restore. Do not spend credits running the same gate again on every push.
        self.assertIn(
            "  run_sandium_fast_regression:\n    type: boolean\n    default: false",
            CONFIG,
        )
        workflow_start = CONFIG.index("  sandium-fast-regression:\n")
        workflow_end = CONFIG.index("  sandium-android-preflight:\n", workflow_start)
        workflow = CONFIG[workflow_start:workflow_end]
        self.assertIn(
            "when: << pipeline.parameters.run_sandium_fast_regression >>",
            workflow,
        )
        stage_one = job_segment("stage-1-source-foundation", "stage-2-graph-and-compile-window")
        self.assertIn("Fast Siso preflight regression gate before Chromium restore", stage_one)

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


    def test_stage_one_prioritizes_cumulative_checkpoint_stages(self):
        segment = job_segment("restore-sandium-build-output", "restore-sandium-siso-cache")
        start = segment.index("Restore highest-stage prior-workflow SANDIUM checkpoint candidate")
        candidate = segment[start:]
        positions = [
            candidate.index(f"sandium-checkpoint-v7-{{{{ .Branch }}}}-stage-{stage}-")
            for stage in (5, 4, 3, 2, 1)
        ]
        self.assertEqual(positions, sorted(positions))

    def test_checkpoint_artifacts_are_published_and_discovery_precedes_restore(self):
        self.assertEqual(CONFIG.count("Publish stage-"), 5)
        self.assertIn("checkpoint_artifact_discovery.py --root", CONFIG)
        self.assertIn("CROSS_WORKFLOW_CHECKPOINT=RESTORED", (ROOT / "ci/siso/checkpoint_artifact_discovery.py").read_text(encoding="utf-8"))

    def test_all_stages_fail_closed_after_checkpoint_miss(self):
        segment = job_segment("restore-sandium-build-output", "restore-sandium-siso-cache")
        self.assertIn('if [[ "$cache_hit" != "true" ]]; then', segment)
        self.assertIn("REQUIRED_PRIOR_WORK_CHECKPOINT=MISS", segment)
        self.assertIn("REFUSING_COLD_REBUILD_IN_EVERY_STAGE=true", segment)
        self.assertIn("exit 42", segment)
        self.assertNotIn("REFUSING_COLD_REBUILD_IN_CONTINUATION_STAGE=true", segment)

    def test_continuation_stages_repair_missing_brave_deps_before_graph_preparation(self):
        command = CONFIG[CONFIG.index("  ensure-sandium-brave-child-dependencies:"):CONFIG.index("  restore-sandium-build-output:")]
        self.assertIn("third_party/bip39wally-core-native/BUILD.gn", command)
        self.assertIn("pnpm run sync --target_os=android --target_arch=arm64 --no-history --sync_chromium=false", command)
        self.assertIn("SANDIUM_CHROMIUM_PIN_PRESERVATION=FAIL", command)
        for current, following in [
            ("stage-3-compile-continuation", "stage-4-finish-and-package"),
            ("stage-4-finish-and-package", "stage-5-verify-publish"),
            ("stage-5-verify-publish", "smoke-1-contract-and-foundation"),
        ]:
            with self.subTest(stage=current):
                segment = job_segment(current, following)
                self.assertIn("- ensure-sandium-brave-child-dependencies", segment)
                self.assertLess(
                    segment.index("- ensure-sandium-brave-child-dependencies"),
                    segment.index("Prepare source overlay and branding before GN"),
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

    def test_full_build_uses_adaptive_compile_budget_with_sub_hour_handover(self):
        # Source restore and GN preparation happen before Stage 1 compilation.
        # Avoid a separate fixed compile cap; allow compilation until the
        # stage budget reaches its five-minute handover reserve.
        self.assertEqual(CONFIG.count("--max-stage-seconds 3300"), 5)
        self.assertEqual(CONFIG.count("--handover-reserve-seconds 300"), 5)
        self.assertEqual(CONFIG.count("3300 - elapsed_seconds - 300"), 5)
        self.assertNotIn("--max-compile-seconds", CONFIG)
        self.assertNotIn("--max-stage-seconds 3000", CONFIG)
        self.assertNotIn("--max-stage-seconds 3900", CONFIG)
        self.assertNotIn("--handover-reserve-seconds 600", CONFIG)
        self.assertNotIn("--max-compile-seconds 2400", CONFIG)
        self.assertNotIn('remaining_compile_seconds=2400', CONFIG)
        self.assertNotIn('remaining_compile_seconds=1800', CONFIG)
    def test_all_stages_provision_autoninja_before_full_build_work(self):
        self.assertIn("ensure-sandium-depot-tools:", CONFIG)
        self.assertEqual(CONFIG.count("- ensure-sandium-depot-tools"), 5)
        self.assertIn(
            "git clone --depth 1 https://chromium.googlesource.com/chromium/tools/depot_tools.git",
            CONFIG,
        )
        self.assertIn("SANDIUM_AUTONINJA_BUILD_ENV_PREFLIGHT=PASS", CONFIG)
        self.assertIn('if [[ ! -s "$DEPOT_TOOLS/python3_bin_reldir.txt" ]]', CONFIG)
        self.assertIn('"$DEPOT_TOOLS/update_depot_tools"', CONFIG)
        self.assertIn("SANDIUM_DEPOT_TOOLS_BOOTSTRAP=PASS", CONFIG)
        self.assertIn('PATH: depotTools + ":"', CONFIG)
        self.assertIn("key: sandium-depot-tools-v1", CONFIG)
        wrapper = (ROOT / "ci/siso/run_compile_window.py").read_text(encoding="utf-8")
        self.assertIn('shutil.which("autoninja", path=env.get("PATH", ""))', wrapper)
        self.assertIn("SANDIUM_AUTONINJA_PREFLIGHT=FAIL", wrapper)

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
