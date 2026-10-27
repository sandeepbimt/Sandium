#!/usr/bin/env python3
"""Tests for stage-specific evidence, identity continuity and local counters."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("sandium_stage_ledger", HERE / "stage_ledger.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def metric_values(**changes):
    values = {
        "records": 4,
        "action_records": 4,
        "remote_executed_action_records": 1,
        "remote_cached_action_records": 1,
        "cached_action_records": 1,
        "local_action_records": 1,
        "local_fallback_action_records": 0,
        "no_exec_action_records": 1,
    }
    values.update(changes)
    return values


class StageLedgerTests(unittest.TestCase):
    def parse_temp(self, metrics_text: str, log_text: str):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            metrics = root / "metrics.jsonl"
            log = root / "siso.log"
            metrics.write_text(metrics_text, encoding="utf-8")
            log.write_text(log_text, encoding="utf-8")
            return MODULE.parse_metrics(metrics, log)

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

    def test_first_two_and_final_stage_do_not_fabricate_siso_evidence(self):
        self.assertEqual(MODULE.validate_stage_evidence(1, None), (False, False))
        self.assertEqual(MODULE.validate_stage_evidence(2, None), (False, False))
        self.assertEqual(MODULE.validate_stage_evidence(5, None), (False, False))
        from build_identity import IdentityError
        for stage in (1, 2, 5):
            with self.subTest(stage=stage), self.assertRaisesRegex(IdentityError, "does not consume Siso"):
                MODULE.validate_stage_evidence(stage, metric_values())

    def test_compile_stage_requires_uncached_remote_execution(self):
        from build_identity import IdentityError
        with self.assertRaisesRegex(IdentityError, "uncached remote action execution"):
            MODULE.validate_stage_evidence(
                3, metric_values(remote_executed_action_records=0, remote_cached_action_records=100)
            )
        self.assertEqual(
            MODULE.validate_stage_evidence(3, metric_values(remote_cached_action_records=0)),
            (True, False),
        )

    def test_continuation_stage_requires_current_invocation_cache_hit(self):
        from build_identity import IdentityError
        with self.assertRaisesRegex(IdentityError, "remote action-cache hits"):
            MODULE.validate_stage_evidence(
                4, metric_values(remote_executed_action_records=200, remote_cached_action_records=0)
            )
        self.assertEqual(
            MODULE.validate_stage_evidence(4, metric_values(remote_executed_action_records=0)),
            (False, True),
        )

    def test_stable_identity_does_not_change_when_generated_gn_identity_appears(self):
        before = {
            "source": "Bare",
            "chromium_commit": "pinned",
            "patch_series_sha256": "patch",
            "gn_args_sha256": None,
        }
        after = {**before, "gn_args_sha256": "generated-gn"}
        self.assertEqual(
            MODULE.digest(MODULE.stable_identity(before)),
            MODULE.digest(MODULE.stable_identity(after)),
        )
        self.assertNotEqual(MODULE.digest(before), MODULE.digest(after))

    def test_stage_two_allows_only_gn_identity_to_be_established(self):
        from build_identity import IdentityError
        source_id = MODULE.digest({"source": "Bare", "chromium_commit": "pinned"})
        previous = {
            "schema": MODULE.SCHEMA,
            "stage": 1,
            "build_identity_sha256": source_id,
            "gn_args_sha256": None,
            "remote_execution_proven": False,
            "remote_cache_reuse_chain_proven": False,
        }
        MODULE.validate_previous(2, previous, source_id, "gn-fingerprint")
        with self.assertRaisesRegex(IdentityError, "changed between stages 1 and 2"):
            MODULE.validate_previous(2, {**previous, "gn_args_sha256": "old"}, source_id, "gn-fingerprint")
        with self.assertRaisesRegex(IdentityError, "must establish"):
            MODULE.validate_previous(2, previous, source_id, None)

    def test_later_stages_require_the_exact_same_gn_identity(self):
        from build_identity import IdentityError
        source_id = MODULE.digest({"source": "Bare", "chromium_commit": "pinned"})
        previous = {
            "schema": MODULE.SCHEMA,
            "stage": 2,
            "build_identity_sha256": source_id,
            "gn_args_sha256": "gn-fingerprint",
            "remote_execution_proven": False,
            "remote_cache_reuse_chain_proven": False,
        }
        MODULE.validate_previous(3, previous, source_id, "gn-fingerprint")
        with self.assertRaisesRegex(IdentityError, "different or missing generated GN"):
            MODULE.validate_previous(3, previous, source_id, "different-gn")

    def test_stage_four_and_five_require_upstream_remote_and_cache_proofs(self):
        from build_identity import IdentityError
        source_id = MODULE.digest({"source": "Bare"})
        previous = {
            "schema": MODULE.SCHEMA,
            "stage": 3,
            "build_identity_sha256": source_id,
            "gn_args_sha256": "gn-fingerprint",
            "remote_execution_proven": False,
            "remote_cache_reuse_chain_proven": False,
        }
        with self.assertRaisesRegex(IdentityError, "actual Siso remote execution"):
            MODULE.validate_previous(4, previous, source_id, "gn-fingerprint")
        previous["remote_execution_proven"] = True
        MODULE.validate_previous(4, previous, source_id, "gn-fingerprint")
        previous["stage"] = 4
        with self.assertRaisesRegex(IdentityError, "remote action-cache reuse"):
            MODULE.validate_previous(5, previous, source_id, "gn-fingerprint")
        previous["remote_cache_reuse_chain_proven"] = True
        MODULE.validate_previous(5, previous, source_id, "gn-fingerprint")

    def test_stage_one_requires_positive_source_readiness_marker(self):
        from build_identity import IdentityError
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "source.log"
            log.write_text("chromium pin configured\n", encoding="utf-8")
            with self.assertRaisesRegex(IdentityError, "lacks a successful preparation"):
                MODULE.validate_source_log(log)
            log.write_text("SANDIUM_CHROMIUM_SOURCE_PREPARATION=PASS\n", encoding="utf-8")
            self.assertEqual(MODULE.validate_source_log(log), MODULE.sha256(log))

    def test_stage_two_requires_generated_gn_args_and_ninja_graph(self):
        from build_identity import IdentityError
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / "src" / "out" / "android_Debug_arm64"
            out.mkdir(parents=True)
            identity = {"gn_args_sha256": "a" * 64}
            (out / "args.gn").write_text("use_siso = true\n", encoding="utf-8")
            with self.assertRaisesRegex(IdentityError, "Ninja build graph is missing"):
                MODULE.validate_build_graph(root, identity)
            (out / "build.ninja").write_text("# graph\n", encoding="utf-8")
            result = MODULE.validate_build_graph(root, identity)
            self.assertEqual(result["gn_args_sha256"], "a" * 64)
            self.assertEqual(result["build_ninja_sha256"], MODULE.sha256(out / "build.ninja"))

    def test_stage_five_requires_manifest_signature_identity_and_matching_hash(self):
        from build_identity import IdentityError
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            apk = root / "Sandium-debug-arm64.apk"
            manifest_path = root / "build-manifest.json"
            apk.write_bytes(b"x" * 1_100_000)
            identity = {"source": "Bare", "gn_args_sha256": "gn-fingerprint"}
            manifest = {
                "build_identity_sha256": MODULE.digest(identity),
                "artifact_name": "Sandium-debug-arm64.apk",
                "artifact_verified": True,
                "apksigner_verification": "PASS",
                "apk_zip_integrity": "PASS",
                "apk_manifest_and_dex": "PASS",
                "artifact_bytes": apk.stat().st_size,
                "artifact_sha256": MODULE.sha256(apk),
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            verified_evidence = {
                "artifact_verified": True,
                "apksigner_verification": "PASS",
                "artifact_bytes": apk.stat().st_size,
                "artifact_sha256": MODULE.sha256(apk),
            }
            with patch.object(MODULE, "verify_apk", return_value=verified_evidence) as verify:
                result = MODULE.validate_artifact(apk, manifest_path, identity, root)
                verify.assert_called_once()
            self.assertEqual(result["artifact_sha256"], MODULE.sha256(apk))
            self.assertEqual(result["apksigner_verification"], "PASS")
            self.assertEqual(result["direct_apk_verification"], "PASS")
            manifest["apksigner_verification"] = "NOT_REQUESTED"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(IdentityError, "apksigner verification"):
                MODULE.validate_artifact(apk, manifest_path, identity)

    def test_ledger_never_adds_prior_stage_action_counts(self):
        identity = {"source": "Bare", "chromium_commit": "pinned", "gn_args_sha256": "gn"}
        identity_hash = MODULE.digest(MODULE.stable_identity(identity))
        current_metrics = metric_values(
            records=65000,
            action_records=55453,
            remote_executed_action_records=500,
            remote_cached_action_records=4000,
            cached_action_records=4000,
            local_action_records=900,
            no_exec_action_records=53,
        )
        previous = {
            "stage": 3,
            "remote_execution_proven": True,
            "remote_cache_reuse_chain_proven": False,
            "stage_action_counts": {"action_records": 103735},
            "siso_metrics": {"action_records": 103735},
        }
        payload = MODULE.build_ledger_payload(
            stage=4,
            identity=identity,
            identity_hash=identity_hash,
            previous=previous,
            metrics=current_metrics,
            previous_ledger_sha256="prev-sha",
            metrics_sha256="metrics-sha",
            log_sha256="log-sha",
            source_log_sha256=None,
            graph_evidence=None,
            artifact_evidence=None,
            remote_execution=False,
            cache_reuse=True,
        )
        self.assertEqual(payload["stage_action_counts"]["action_records"], 55453)
        self.assertEqual(payload["stage_action_counts"]["scope"], "current_siso_invocation_only")
        self.assertEqual(payload["action_count_scope"], "STAGE_LOCAL_ONLY_NOT_CUMULATIVE")
        self.assertEqual(payload["siso_metrics"]["action_records"], 55453)
        self.assertTrue(payload["remote_execution_proven"])
        self.assertFalse(payload["current_stage_remote_execution_proven"])
        self.assertTrue(payload["remote_cache_reuse_proven"])
        self.assertTrue(payload["remote_cache_reuse_chain_proven"])
        self.assertNotIn("cumulative_completed_actions", payload)
        self.assertNotIn("max_completed_actions", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
