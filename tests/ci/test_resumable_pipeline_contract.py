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

    def test_stage1_bootstrap_is_opt_in_and_does_not_schedule_continuation(self):
        self.assertIn(
            "  run_sandium_stage1_bootstrap:\n    type: boolean\n    default: false",
            CONFIG,
        )
        start = CONFIG.index("  sandium-stage1-bootstrap:\n")
        end = CONFIG.index("  sandium-android-preflight:\n", start)
        workflow = CONFIG[start:end]
        self.assertIn("<< pipeline.parameters.run_sandium_stage1_bootstrap >>", workflow)
        self.assertNotIn("<< pipeline.parameters.run_sandium_compile_windows >>", workflow)
        self.assertIn("not: << pipeline.parameters.run_sandium_android_apk_build >>", workflow)
        self.assertIn("      - stage-1-source-foundation", workflow)
        self.assertNotIn("stage-2-graph-and-compile-window", workflow)
        # Stage-1 bootstrap with compile_windows=false must still compile and
        # checkpoint; only ordinary preflight runs may skip the compile window.
        stage_one = job_segment("stage-1-source-foundation", "stage-2-graph-and-compile-window")
        self.assertIn(
            'if [[ "<< pipeline.parameters.run_sandium_compile_windows >>" != "true" && "<< pipeline.parameters.run_sandium_stage1_bootstrap >>" != "true" ]]; then',
            stage_one,
        )
        self.assertIn("--stage stage-1 --require-siso-state", stage_one)

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

    def test_production_stage_one_accepts_compatible_output_only_checkpoints(self):
        segment = job_segment("restore-sandium-build-output", "restore-sandium-siso-cache")
        self.assertIn('checkpoint_artifact_discovery.py --root "$HOME/project"', segment)
        self.assertNotIn("--require-siso-state", segment)
        self.assertIn("SANDIUM_RESUME_MODE=INCREMENTAL_FROM_RESTORED_OUTPUTS", segment)

    def test_audited_output_only_checkpoint_authorizes_incremental_output_reuse(self):
        segment = job_segment("restore-sandium-build-output", "restore-sandium-siso-cache")
        output_only = segment[segment.index('if [[ "$DISCOVERY_STATUS" == "RESTORED_OUTPUTS_ONLY" ]]; then', segment.index("Validate restored Android build output")):]
        self.assertIn("SANDIUM_OUTPUT_ONLY_GN_IDENTITY=PASS", output_only)
        self.assertIn("SANDIUM_CHECKPOINT_RESUME=INCREMENTAL_FROM_RESTORED_OUTPUTS", output_only)
        self.assertIn("SANDIUM_COMPILE_AUTHORIZATION=INCREMENTAL_OUTPUTS_ONLY", output_only)
        self.assertIn("export SANDIUM_BUILD_OUTPUT_CACHE_HIT=true", output_only)
        self.assertIn("export SANDIUM_SISO_STATE_RESTORED=false", output_only)
        self.assertLess(output_only.index("SANDIUM_OUTPUT_ONLY_GN_IDENTITY=PASS"),
                        output_only.index("export SANDIUM_BUILD_OUTPUT_CACHE_HIT=true"))
        self.assertIn("SANDIUM_CHECKPOINT_SISO_STATE=DISCARDED_FOR_SAFE_REINITIALIZATION", output_only)
        self.assertIn("SANDIUM_COMPILE_AUTHORIZATION=NOT_GRANTED_VALIDATION_ONLY", output_only)
        self.assertIn('CIRCLE_JOB:-}" == "stage-1-source-foundation"', output_only)

    def test_output_only_recovery_is_stage1_only_and_checks_effective_siso_configuration(self):
        segment = job_segment("restore-sandium-build-output", "restore-sandium-siso-cache")
        self.assertIn('if [[ "$STAGE_INDEX" != "1" ]]; then', segment)
        self.assertIn('CIRCLE_JOB:-}" == "stage-1-source-foundation"', segment)
        self.assertIn('CIRCLE_JOB:-}" == "sandium-checkpoint-restore-validation"', segment)
        self.assertIn('expected_gn = {"use_siso": "true", "use_reclient": "false"}', segment)
        self.assertIn('"--use_remoteexec", "true"', segment)
        self.assertIn('"--gn", "use_siso:true"', segment)
        self.assertIn('"--gn", "use_reclient:false"', segment)
        self.assertNotIn('expected_gn = {"use_remoteexec": "true"', segment)
        self.assertIn("SANDIUM_GN_ARGS_QUERY_DIAGNOSTIC_BEGIN", segment)
        self.assertIn("SANDIUM_GN_ARGS_QUERY_DIAGNOSTIC_END", segment)
        self.assertIn("gn_query.returncode", segment)
        self.assertIn("not os.access(gn, os.X_OK)", segment)
        self.assertIn("not os.access(args, os.R_OK)", segment)
        self.assertIn('source_root = root / "src"', segment)
        self.assertIn('if not (source_root / ".gn").is_file():', segment)
        self.assertIn('cwd=str(source_root)', segment)
        self.assertIn('version_utils = brave / "script" / "brave_version_utils.py"', segment)
        self.assertIn('if not version_utils.is_file():', segment)
        self.assertIn('query_env["PYTHONPATH"] = str(version_utils.parent)', segment)
        self.assertIn('env=query_env', segment)
        self.assertIn('python3 build/util/version.py gen ../chrome/VERSION', segment)
        self.assertIn('SANDIUM_BRAVE_VERSION_GNI=GENERATED_FOR_VALIDATION', segment)
        self.assertIn('test -s "$BRAVE/build/version.gni"', segment)
        self.assertIn('pnpm run build Debug --target_os=android --target_arch=arm64 --target_android_output_format=apk --use_remoteexec true --gn use_siso:true --gn use_reclient:false --prepare_only', segment)
        self.assertIn('SANDIUM_CHECKPOINT_OVERLAY_PREPARE_ONLY=PASS', segment)
        self.assertIn('OUTPUT_ONLY_COMPATIBILITY=FAIL Brave source-overlay prepare-only failed', segment)

    def test_checkpoint_artifacts_are_published_and_discovery_precedes_restore(self):
        self.assertEqual(CONFIG.count("Publish stage-"), 5)
        self.assertIn("checkpoint_artifact_discovery.py --root", CONFIG)
        self.assertIn("CROSS_WORKFLOW_CHECKPOINT=RESTORED", (ROOT / "ci/siso/checkpoint_artifact_discovery.py").read_text(encoding="utf-8"))

    def test_preflight_only_stages_never_pack_or_publish_resumable_checkpoints(self):
        for stage in range(1, 6):
            with self.subTest(stage=stage):
                pack_start = CONFIG.index(f"name: Pack stage-{stage} cumulative Siso checkpoint")
                pack_end = CONFIG.index(f"name: Publish stage-{stage} checkpoint files", pack_start)
                pack = CONFIG[pack_start:pack_end]
                self.assertIn('run_sandium_compile_windows >>" != "true"', pack)
                self.assertIn("SANDIUM_CHECKPOINT_HANDOVER=NO_COMPILE_NO_CHECKPOINT", pack)
                publish_start = pack_end
                publish_end = CONFIG.find("\n      - ", publish_start + 1)
                publish = CONFIG[publish_start:publish_end if publish_end >= 0 else len(CONFIG)]
                self.assertIn('run_sandium_compile_windows >>" != "true"', publish)
                self.assertIn("CHECKPOINT_ARTIFACT_PUBLISH_SKIPPED", publish)

    def test_output_only_cache_reuses_compatible_compiled_outputs(self):
        segment = job_segment("restore-sandium-build-output", "restore-sandium-siso-cache")
        self.assertIn('SISO_STATE="$OUT/.siso_fs_state"', segment)
        self.assertIn('SISO_JOURNAL="$OUT/.siso_fs_state.journal"', segment)
        normalized = " ".join(segment.split())
        self.assertIn('siso_checkpoint_state_safe=false', segment)
        self.assertIn('"$journal_entry_exists" == "false" || "$siso_journal_valid" == "true"', normalized)
        self.assertIn('SANDIUM_RESUME_MODE=FULL_SISO_STATE_AND_OUTPUTS', segment)
        self.assertIn('SANDIUM_RESUME_MODE=INCREMENTAL_FROM_RESTORED_OUTPUTS', segment)
        self.assertIn("BUILD_OUTPUT_CACHE=COMPATIBLE_OUTPUTS_ONLY", segment)
        self.assertIn('if [[ -d "$OUT" ]] &&', segment)
        self.assertNotIn('if [[ -d "$OUT" && "$siso_pair_valid" == "true" ]]', segment)

    def test_stage_one_refuses_cold_start_when_prior_work_is_missing(self):
        segment = job_segment("restore-sandium-build-output", "restore-sandium-siso-cache")
        self.assertIn("SANDIUM_CHECKPOINT_RESUME=RESTORED_PRIOR_COMPILED_WORK", segment)
        self.assertIn('grep -Fxq "NO_COMPATIBLE_CANDIDATE" .sandium-checkpoint-discovery-status', segment)
        self.assertIn('grep -Fxq "NO_RESUMABLE_SISO_CANDIDATE" .sandium-checkpoint-discovery-status', segment)
        self.assertIn("no compatible prior-work checkpoint exists; cold start is prohibited", segment)
        self.assertIn("REQUIRED_PRIOR_WORK_CHECKPOINT=MISS", segment)
        self.assertIn("REFUSING_UNVERIFIED_COLD_REBUILD=true", segment)
        self.assertIn("exit 42", segment)
        self.assertNotIn('rm -rf "$OUT"', segment)
        stage_one = job_segment("stage-1-source-foundation", "stage-2-graph-and-compile-window")
        self.assertNotIn("- restore-sandium-apk-cache", stage_one)
        self.assertNotIn("Exit Stage 1 immediately when a verified APK is reusable", stage_one)

    def test_checkpoint_discovery_only_authorizes_cold_start_after_complete_search(self):
        discovery = (ROOT / "ci/siso/checkpoint_artifact_discovery.py").read_text(encoding="utf-8")
        self.assertIn('write_text("NO_COMPATIBLE_CANDIDATE', discovery)
        self.assertIn("DISCOVERY_INCOMPLETE", discovery)

    def test_compile_window_reports_residual_work_not_a_raw_task_ceiling(self):
        wrapper = (ROOT / "ci/siso/run_compile_window.py").read_text(encoding="utf-8")
        self.assertIn('TASK_PROGRESS = re.compile(r"^\\[(\\d+)/(\\d+)\\]")', wrapper)
        self.assertIn("latest_progress_completed", wrapper)
        self.assertIn("NINJA_ESTIMATED_REMAINING", wrapper)
        self.assertIn("TASK_COUNT_POLICY=REPORT_RESIDUAL_NOT_RAW_DENOMINATOR", wrapper)
        self.assertNotIn("latest_progress_total > 115000", wrapper)
        self.assertNotIn("REMAINING_TASK_BUDGET_GATE", wrapper)

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

    def test_continuation_stages_recheck_brave_gn_inputs_immediately_before_compile(self):
        for current, following, stage_number in [
            ("stage-3-compile-continuation", "stage-4-finish-and-package", 3),
            ("stage-4-finish-and-package", "stage-5-verify-publish", 4),
            ("stage-5-verify-publish", "smoke-1-contract-and-foundation", 5),
        ]:
            with self.subTest(stage=current):
                segment = job_segment(current, following)
                gate = segment.index("# Recheck critical Brave GN inputs immediately before Siso starts.")
                compile_window = segment.index("run_compile_window.py")
                self.assertLess(gate, compile_window)
                gate_text = segment[gate:compile_window]
                self.assertIn("SANDIUM_PRECOMPILE_INPUT_GATE=FAIL missing=", gate_text)
                self.assertIn("bip39wally-core-native/BUILD.gn", gate_text)
                self.assertIn("bat-native-tweetnacl/BUILD.gn", gate_text)
                self.assertIn('"$HOME/project/$OUT/build.ninja"', gate_text)
                self.assertIn('"$HOME/project/$OUT/build.ninja.stamp"', gate_text)
                self.assertIn("SANDIUM_PRECOMPILE_INPUT_GATE=PASS", gate_text)

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
        # stage budget reaches its ten-minute handover reserve.
        self.assertEqual(CONFIG.count("--max-stage-seconds 3300"), 5)
        self.assertEqual(CONFIG.count("--handover-reserve-seconds 600"), 5)
        self.assertEqual(CONFIG.count("3300 - elapsed_seconds - 600"), 5)
        self.assertNotIn("--max-compile-seconds", CONFIG)
        self.assertNotIn("--max-stage-seconds 3000", CONFIG)
        self.assertNotIn("--max-stage-seconds 3900", CONFIG)
        self.assertNotIn("--remote-execution-grace-seconds", CONFIG)
        self.assertIn("REMOTE_EXECUTION_STARTUP_DEADLINE_ACTIVE=false", (ROOT / "ci/siso/run_compile_window.py").read_text(encoding="utf-8"))
        self.assertEqual(CONFIG.count("--shutdown-grace-seconds 360"), 5)
        self.assertIn("SANDIUM_USE_GOOGLECHROME_SISO_CONFIG=false", CONFIG)
        self.assertIn("siso_use_googlechrome_config=false", CONFIG)
        self.assertIn("SANDIUM_SISORC_REGENERATED_FROM_RUNTIME_ENV=true", CONFIG)
        self.assertIn("SANDIUM_SISO_GOOGLECHROME_POLICY=disabled_for_external_buildbuddy", CONFIG)
        self.assertNotIn("export SANDIUM_USE_GOOGLECHROME_SISO_CONFIG=true", CONFIG)
        self.assertNotIn("--handover-reserve-seconds 300", CONFIG)
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

    def test_compile_window_reports_siso_state_persistence_before_and_after_shutdown(self):
        wrapper = (ROOT / "ci/siso/run_compile_window.py").read_text(encoding="utf-8")
        self.assertIn("def state_file_snapshot(", wrapper)
        self.assertIn('emit_state_snapshot(log, out_dir, "STAGE_START")', wrapper)
        self.assertIn('emit_state_snapshot(log, out_dir, "STAGE_END")', wrapper)
        self.assertIn('emit(log, f"{phase}_SISO_CHECKPOINT_STATE_SAFE={str(state_safe).lower()}")', wrapper)
        self.assertIn("SISO_STATE_TRANSITION_", wrapper)
        self.assertIn("SISO_SHUTDOWN_FORCED_KILL=", wrapper)
        self.assertIn("SISO_STATE_PERSISTENCE_RESULT=BASE_STATE_MISSING_OR_INVALID_AFTER_SHUTDOWN", wrapper)
        self.assertIn("STATE_UNCHANGED_CLEAN_SHUTDOWN_JOURNAL_NOT_REQUIRED", wrapper)
        self.assertIn("STATE_UPDATED_CLEAN_SHUTDOWN_JOURNAL_NOT_REQUIRED", wrapper)

    def test_state_only_clean_shutdown_checkpoint_is_resumable_across_all_layers(self):
        checkpoint = (ROOT / "ci/siso/checkpoint.py").read_text(encoding="utf-8")
        runner = (ROOT / "ci/siso/run_compile_window.py").read_text(encoding="utf-8")
        catalog = (ROOT / "ci/siso/checkpoint_catalog.py").read_text(encoding="utf-8")
        discovery = (ROOT / "ci/siso/checkpoint_artifact_discovery.py").read_text(encoding="utf-8")
        self.assertIn("the journal is optional after clean shutdown", checkpoint)
        self.assertIn("STATE_SAVED_CLEAN_SHUTDOWN_JOURNAL_NOT_REQUIRED", runner)
        self.assertIn("journal_valid_or_absent", catalog)
        self.assertIn("journal_valid_or_absent", discovery)
        restore = job_segment("restore-sandium-build-output", "restore-sandium-siso-cache")
        self.assertIn('"$journal_entry_exists" == "false" || "$siso_journal_valid" == "true"', " ".join(restore.split()))
        self.assertIn("SANDIUM_CHECKPOINT_RESUMABILITY=RESUMABLE_SISO_STATE", CONFIG)
        self.assertNotIn('all(siso_present)', CONFIG)

    def test_compile_window_has_early_remote_gate_and_graceful_shutdown(self):
        wrapper = (ROOT / "ci/siso/run_compile_window.py").read_text(encoding="utf-8")
        self.assertIn("REMOTE_EXECUTION_STARTUP_DEADLINE_ACTIVE=false", wrapper)
        self.assertIn("REMOTE_EXECUTION_POLICY=WORKFLOW_PREFLIGHT_PLUS_STRICT_REMOTE_AND_FINAL_METRICS", wrapper)
        self.assertNotIn("remote_action_deadline_expired=true", wrapper)
        self.assertNotIn("--remote-execution-grace-seconds", CONFIG)
        build = (ROOT / "build/commands/lib/build.ts").read_text(encoding="utf-8")
        self.assertIn("config.extraNinjaOpts.push('-strict_remote', '-metrics_json', metricsPath, `-j${jobs}`)", build)
        self.assertIn("SISO_GRACEFUL_SHUTDOWN_REQUESTED=SIGINT", wrapper)
        self.assertIn("SISO_SHUTDOWN_ESCALATION=SIGTERM", wrapper)
        self.assertIn("remote_cached_action_records", wrapper)
        self.assertIn("fast_local_action_records", wrapper)

    def test_siso_action_cache_is_shared_across_branches(self):
        self.assertIn("sandium-siso-v4-android-debug-arm64-", CONFIG)
        for stage in range(1, 6):
            self.assertIn(f"key: sandium-siso-v4-android-debug-arm64-stage{stage}-", CONFIG)


if __name__ == "__main__":
    unittest.main()
