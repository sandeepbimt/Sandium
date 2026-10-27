#!/usr/bin/env python3
"""Tests for stage-local Siso evidence and non-cumulative progress accounting."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("sandium_stage_ledger", HERE / "stage_ledger.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class StageLedgerTests(unittest.TestCase):
    def parse_temp(self, metrics_text: str, log_text: str):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        metrics = root / "metrics.jsonl"
        log = root / "siso.log"
        metrics.write_text(metrics_text, encoding="utf-8")
        log.write_text(log_text, encoding="utf-8")
        try:
            parsed = MODULE.parse_metrics(metrics, log)
        finally:
            temporary.cleanup()
        return parsed

    def test_structured_metrics_count_only_the_current_invocation(self):
        rows = [
            {"output": "obj/a.o", "is_remote": True, "cached": False},
            {"output": "obj/b.o", "is_remote": True, "cached": True},
            {"output": "obj/c.o", "is_local": True, "cached": False},
            {"output": "phony/noop", "is_remote": True, "cached": False, "no_exec": True},
        ]
        metrics = self.parse_temp(
            "".join(json.dumps(row) + "\n" for row in rows),
            "Siso completed without a summary counter",
        )
        self.assertEqual(metrics["action_records"], 4)
        self.assertEqual(metrics["remote_executed_action_records"], 1)
        self.assertEqual(metrics["remote_cached_action_records"], 1)
        self.assertEqual(metrics["local_action_records"], 1)
        self.assertEqual(metrics["no_exec_action_records"], 1)

    def test_summary_fallback_parses_counts_as_one_invocation(self):
        metrics = self.parse_temp(
            "",
            "Siso finished local:8 remote:81 cache:103 cache-write:4(err:0) fallback:2 retry:1 skip:5",
        )
        self.assertEqual(metrics["local_action_records"], 8)
        self.assertEqual(metrics["remote_executed_action_records"], 81)
        self.assertEqual(metrics["remote_cached_action_records"], 103)
        self.assertEqual(metrics["local_fallback_action_records"], 2)

    def test_endpoint_or_text_without_action_evidence_is_not_reuse_proof(self):
        from build_identity import IdentityError
        with self.assertRaises(IdentityError):
            self.parse_temp("", 'use reapi "remote.buildbuddy.io:443"\nremote cache configured')

    def test_stage_one_requires_actual_uncached_remote_execution(self):
        from build_identity import IdentityError
        metrics = {
            "remote_executed_action_records": 0,
            "remote_cached_action_records": 100,
        }
        with self.assertRaisesRegex(IdentityError, "uncached remote action execution"):
            MODULE.validate_stage_evidence(1, metrics, None)

    def test_continuation_stage_requires_current_stage_cache_hit(self):
        from build_identity import IdentityError
        previous = {"stage": 1, "remote_execution_proven": True}
        no_current_hit = {
            "remote_executed_action_records": 200,
            "remote_cached_action_records": 0,
        }
        with self.assertRaisesRegex(IdentityError, "remote action-cache hits"):
            MODULE.validate_stage_evidence(2, no_current_hit, previous)
        hit = {**no_current_hit, "remote_cached_action_records": 1}
        self.assertEqual(MODULE.validate_stage_evidence(2, hit, previous), (True, True))

    def test_final_stage_accepts_remote_execution_or_cache_hit(self):
        local_only = {"remote_executed_action_records": 0, "remote_cached_action_records": 0}
        from build_identity import IdentityError
        with self.assertRaisesRegex(IdentityError, "neither remote execution nor remote-cache evidence"):
            MODULE.validate_stage_evidence(5, local_only, None)
        self.assertEqual(
            MODULE.validate_stage_evidence(5, {**local_only, "remote_cached_action_records": 1}, None),
            (False, True),
        )

    def test_ledger_does_not_add_prior_stage_action_counts(self):
        identity = {"source": "Bare", "chromium_commit": "pinned"}
        identity_hash = "identity-sha"
        current_metrics = {
            "records": 65000,
            "action_records": 55453,
            "remote_executed_action_records": 500,
            "remote_cached_action_records": 4000,
            "cached_action_records": 4000,
            "local_action_records": 900,
            "local_fallback_action_records": 0,
            "no_exec_action_records": 53,
        }
        # The previous ledger is intentionally much larger. Those counts describe
        # a different Siso invocation and must never be added to this stage.
        previous = {
            "stage": 1,
            "remote_execution_proven": True,
            "stage_action_counts": {"action_records": 103735},
            "siso_metrics": {"action_records": 103735},
        }
        payload = MODULE.build_ledger_payload(
            stage=2,
            identity=identity,
            identity_hash=identity_hash,
            previous=previous,
            metrics=current_metrics,
            previous_ledger_sha256="prev-sha",
            metrics_sha256="metrics-sha",
            log_sha256="log-sha",
            remote_execution=False,
            cache_reuse=True,
        )
        self.assertEqual(payload["stage_action_counts"]["action_records"], 55453)
        self.assertEqual(payload["stage_action_counts"]["scope"], "current_siso_invocation_only")
        self.assertEqual(payload["action_count_scope"], "STAGE_LOCAL_ONLY_NOT_CUMULATIVE")
        self.assertEqual(payload["siso_metrics"]["action_records"], 55453)
        self.assertTrue(payload["remote_execution_proven"])  # chain-level fact only
        self.assertFalse(payload["current_stage_remote_execution_proven"])
        self.assertTrue(payload["remote_cache_reuse_proven"])
        self.assertNotIn("cumulative_completed_actions", payload)
        self.assertNotIn("max_completed_actions", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
