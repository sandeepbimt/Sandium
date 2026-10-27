#!/usr/bin/env python3
"""No-build regression tests for schema-2 stage-runner wiring and ledger handoff."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

CONTROL = Path(__file__).resolve().parents[2]
BARE = CONTROL / "ci" / "bare"
RUNNER = (BARE / "run_stage.sh").read_text(encoding="utf-8")
CONFIG = (CONTROL / ".circleci" / "config.yml").read_text(encoding="utf-8")
SPEC = importlib.util.spec_from_file_location("sandium_schema2_stage_ledger", BARE / "stage_ledger.py")
assert SPEC is not None and SPEC.loader is not None
LEDGER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LEDGER)


class StageRunnerContractTests(unittest.TestCase):
    def test_runner_has_explicit_opt_in_and_is_not_wired_to_build_workflow(self):
        self.assertIn('SANDIUM_BARE_STAGE_RUN_APPROVED:-false', RUNNER)
        self.assertIn('SANDIUM_BARE_STAGE_RUN=BLOCKED', RUNNER)
        self.assertIn('run_sandium_bare_source_ready:\n    type: boolean\n    default: false', CONFIG)
        self.assertIn('run_sandium_bare_android_apk_build:\n    type: boolean\n    default: false', CONFIG)
        self.assertIn('SANDIUM_BARE_FULL_BUILD=BLOCKED', CONFIG)

    def test_stage_one_is_source_readiness_only(self):
        self.assertIn('--stage 1 --source-log "$SOURCE_LOG" --output "$LEDGER" --root "$ROOT"', RUNNER)
        self.assertIn('GN_GENERATION_RUN=no', RUNNER)
        self.assertIn('SISO_BUILD_RUN=no', RUNNER)
        stage_one = RUNNER.split('  1)\n', 1)[1].split('  2)\n', 1)[0]
        self.assertNotIn('--metrics', stage_one)
        self.assertNotIn('autoninja', stage_one)
        self.assertNotIn('bootstrap_chromium.sh" configure', stage_one)

    def test_stage_two_records_gn_graph_without_siso_metrics(self):
        stage_two = RUNNER.split('  2)\n', 1)[1].split('  3|4)\n', 1)[0]
        self.assertIn('bootstrap_chromium.sh" configure', stage_two)
        self.assertIn('need_file "$OUT/args.gn"', stage_two)
        self.assertIn('need_file "$OUT/build.ninja"', stage_two)
        self.assertIn('--stage 2 --previous "$PREV" --output "$LEDGER" --root "$ROOT"', stage_two)
        self.assertNotIn('--metrics', stage_two)
        self.assertNotIn('autoninja', stage_two)

    def test_only_stages_three_and_four_consume_current_invocation_metrics(self):
        compile_block = RUNNER.split('  3|4)\n', 1)[1].split('  5)\n', 1)[0]
        self.assertIn('--metrics "$METRICS" --siso-log "$SISO_LOG"', compile_block)
        self.assertIn('--previous "$PREV" --output "$LEDGER"', compile_block)
        self.assertIn('autoninja -strict_remote -metrics_json', compile_block)
        self.assertIn('SANDIUM_STAGE_BUILD_SECONDS:-2700', compile_block)

    def test_stage_five_verifies_artifact_without_siso_metrics(self):
        stage_five = RUNNER.split('  5)\n', 1)[1].split('esac', 1)[0]
        self.assertIn('package_artifact.sh', stage_five)
        self.assertIn('--artifact "$ROOT/sandium-output/Sandium-debug-arm64.apk"', stage_five)
        self.assertIn('--manifest "$ROOT/sandium-output/build-manifest.json"', stage_five)
        self.assertNotIn('--metrics', stage_five)
        self.assertNotIn('autoninja', stage_five)

    def test_no_stale_schema_one_ledger_arguments_remain(self):
        for obsolete in (
            '--completed-actions', '--total-actions', '--source-timestamp-policy',
            '--state-handoff-report', '--apk ',
        ):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, RUNNER)

    def test_later_stages_fail_closed_when_previous_ledger_or_workspace_is_missing(self):
        self.assertIn('need_file "$PREV"', RUNNER)
        self.assertIn('stage workspace persistence is incomplete', RUNNER)
        self.assertIn('pinned Chromium checkout is not present in the restored stage workspace', RUNNER)
        self.assertIn('module.validate_previous(', RUNNER)


class Schema2LedgerHandoffTests(unittest.TestCase):
    def _payload(self, stage, identity, identity_hash, previous=None, remote=False, cache=False):
        metrics = None
        if stage in (3, 4):
            metrics = {
                "records": 2,
                "action_records": 2,
                "remote_executed_action_records": 1 if remote else 0,
                "remote_cached_action_records": 1 if cache else 0,
                "cached_action_records": 1 if cache else 0,
                "local_action_records": 0,
                "local_fallback_action_records": 0,
                "no_exec_action_records": 0,
            }
        return LEDGER.build_ledger_payload(
            stage=stage,
            identity=identity,
            identity_hash=identity_hash,
            previous=previous,
            metrics=metrics,
            previous_ledger_sha256=("f" * 64 if previous else None),
            metrics_sha256=("a" * 64 if stage in (3, 4) else None),
            log_sha256=("b" * 64 if stage in (3, 4) else None),
            source_log_sha256=("c" * 64 if stage == 1 else None),
            graph_evidence=({"gn_args_sha256": "d" * 64, "build_ninja_sha256": "e" * 64} if stage == 2 else None),
            artifact_evidence=({"direct_apk_verification": "PASS"} if stage == 5 else None),
            remote_execution=remote,
            cache_reuse=cache,
        )

    def test_valid_five_stage_identity_and_evidence_chain_is_accepted(self):
        source_id = {"source": "Bare", "chromium_commit": "pinned"}
        source_hash = LEDGER.digest(source_id)
        gn_identity = {**source_id, "gn_args_sha256": "d" * 64}
        gn_identity_hash = LEDGER.digest(LEDGER.stable_identity(gn_identity))

        one = self._payload(1, {**source_id, "gn_args_sha256": None}, source_hash)
        self.assertEqual(source_hash, gn_identity_hash)
        LEDGER.validate_previous(2, one, gn_identity_hash, gn_identity["gn_args_sha256"])
        two = self._payload(2, gn_identity, gn_identity_hash, one)

        LEDGER.validate_previous(3, two, gn_identity_hash, gn_identity["gn_args_sha256"])
        three = self._payload(3, gn_identity, gn_identity_hash, two, remote=True)

        LEDGER.validate_previous(4, three, gn_identity_hash, gn_identity["gn_args_sha256"])
        four = self._payload(4, gn_identity, gn_identity_hash, three, remote=True, cache=True)

        LEDGER.validate_previous(5, four, gn_identity_hash, gn_identity["gn_args_sha256"])
        five = self._payload(5, gn_identity, gn_identity_hash, four, remote=True, cache=True)
        self.assertEqual([one["stage"], two["stage"], three["stage"], four["stage"], five["stage"]], [1, 2, 3, 4, 5])
        self.assertTrue(three["remote_execution_proven"])
        self.assertTrue(four["remote_execution_proven"])
        self.assertTrue(four["remote_cache_reuse_chain_proven"])

    def test_wrong_stage_order_identity_or_missing_stage_four_cache_proof_is_rejected(self):
        from build_identity import IdentityError

        identity = {"source": "Bare", "chromium_commit": "pinned", "gn_args_sha256": "d" * 64}
        stable_hash = LEDGER.digest(LEDGER.stable_identity(identity))
        previous = {
            "schema": LEDGER.SCHEMA,
            "stage": 3,
            "build_identity_sha256": stable_hash,
            "gn_args_sha256": identity["gn_args_sha256"],
            "remote_execution_proven": True,
            "remote_cache_reuse_chain_proven": False,
        }
        with self.assertRaisesRegex(IdentityError, "order mismatch"):
            LEDGER.validate_previous(5, previous, stable_hash, identity["gn_args_sha256"])
        with self.assertRaisesRegex(IdentityError, "different stable source"):
            LEDGER.validate_previous(4, previous, "0" * 64, identity["gn_args_sha256"])
        previous["stage"] = 4
        with self.assertRaisesRegex(IdentityError, "remote action-cache reuse"):
            LEDGER.validate_previous(5, previous, stable_hash, identity["gn_args_sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
