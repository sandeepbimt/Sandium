"""Tests for strict-remote preflight action evidence and marker scope."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ci/siso/remote_preflight.py"
spec = importlib.util.spec_from_file_location("sandium_remote_preflight", MODULE_PATH)
assert spec and spec.loader
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def record(output: str, **flags):
    value = {"step_id": "test-step", "rule": "clang/cxx", "action": "cxx", "outputs": [output]}
    value.update(flags)
    return value


class RemotePreflightTests(unittest.TestCase):
    def parse(self, records):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.jsonl"
            path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            return preflight.parse_remote_probe_metrics(path)

    def test_generated_siso_environment_checks_endpoint_without_assuming_instance_flag(self):
        preflight.validate_generated_siso_environment("remote.buildbuddy.io:443")
        with self.assertRaisesRegex(ValueError, "remote.buildbuddy.io:443"):
            preflight.validate_generated_siso_environment("no remote endpoint")

    def test_authoritative_runtime_config_checks_default_instance(self):
        preflight.validate_buildbuddy_runtime_config(
            "rbe_service=remote.buildbuddy.io:443\\n"
            "projects_chrome_custom_vars_reapi_address=remote.buildbuddy.io:443\\n"
            "projects_chrome_custom_vars_reapi_instance=default\\n"
            "use_remoteexec=true\\n"
        )
        with self.assertRaisesRegex(ValueError, "reapi_instance=default"):
            preflight.validate_buildbuddy_runtime_config(
                "rbe_service=remote.buildbuddy.io:443\\n"
                "projects_chrome_custom_vars_reapi_address=remote.buildbuddy.io:443\\n"
                "projects_chrome_custom_vars_reapi_instance=sandium/android-arm64/ci\\n"
                "use_remoteexec=true\\n"
            )

    def test_probe_rule_is_found_in_generated_subninja_fragment(self):
        with tempfile.TemporaryDirectory() as directory:
            fragment = Path(directory) / "test_support.ninja"
            fragment.write_text(
                "build obj/base/test/test_support/mock_log.o "
                "obj/base/test/test_support/mock_log.dwo: cxx "
                "../../base/test/mock_log.cc\n",
                encoding="utf-8",
            )
            self.assertTrue(preflight.has_probe_rule(fragment))
            fragment.write_text(
                "build obj/base/test/test_support/other.o: cxx "
                "../../base/test/other.cc\n",
                encoding="utf-8",
            )
            self.assertFalse(preflight.has_probe_rule(fragment))

    def test_probe_rule_rejects_missing_fragment(self):
        with tempfile.TemporaryDirectory() as directory:
            fragment = Path(directory) / "missing.ninja"
            self.assertFalse(preflight.has_probe_rule(fragment))

    def test_accepts_only_uncached_remote_probe_output(self):
        metrics = self.parse([
            record("obj/base/test/test_support/mock_log.o", is_remote=True, cached=False, no_exec=False),
        ])
        self.assertEqual(metrics["target_action_records"], 1)
        self.assertEqual(metrics["uncached_remote_target_actions"], 1)

    def test_rejects_cache_hit_and_local_results(self):
        cached = self.parse([record("obj/base/test/test_support/mock_log.o", is_remote=True, cached=True)])
        local = self.parse([record("obj/base/test/test_support/mock_log.o", is_local=True)])
        self.assertEqual(cached["uncached_remote_target_actions"], 0)
        self.assertEqual(local["uncached_remote_target_actions"], 0)

    def test_rejects_unrelated_remote_output_and_invalid_metrics(self):
        other = self.parse([record("obj/base/atomic_flag.o", is_remote=True, cached=False)])
        self.assertEqual(other["uncached_remote_target_actions"], 0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.jsonl"
            path.write_text("{not-json}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid Siso metrics JSONL"):
                preflight.parse_remote_probe_metrics(path)

    def test_marker_must_match_workflow_and_both_source_identities(self):
        marker = {
            "schema": 1, "status": "PASS", "workflow_id": "workflow-a",
            "source_commit": "brave-sha", "chromium_commit": "chromium-sha",
            "chromium_version": "156.0.8078.39", "probe_target": "obj/base/test/test_support/mock_log.o",
            "probe_output_suffix": "obj/base/test/test_support/mock_log.o", "probe_remote_action_records": 1,
            "metrics_sha256": "a" * 64, "siso_state_pair_valid": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "marker.json"
            path.write_text(json.dumps(marker), encoding="utf-8")
            loaded = preflight.load_valid_marker(path, "workflow-a", "brave-sha", "chromium-sha", "156.0.8078.39")
            self.assertEqual(loaded["probe_remote_action_records"], 1)
            with self.assertRaisesRegex(ValueError, "workflow_id"):
                preflight.load_valid_marker(path, "workflow-b", "brave-sha", "chromium-sha", "156.0.8078.39")


if __name__ == "__main__":
    unittest.main(verbosity=2)
