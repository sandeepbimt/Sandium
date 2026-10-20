#!/usr/bin/env python3
"""Regression tests for Siso metrics emitted by the pinned Siso JSONL writer."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "ci" / "siso" / "run_compile_window.py"
SPEC = importlib.util.spec_from_file_location("sandium_run_compile_window", RUNNER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Cannot load compile-window runner from {RUNNER_PATH}")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class SisoMetricsParserTests(unittest.TestCase):
    def parse_records(self, records: list[dict]) -> dict[str, int]:
        with tempfile.TemporaryDirectory() as directory:
            metrics_path = Path(directory) / "metrics.jsonl"
            metrics_path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            return RUNNER.parse_metrics(metrics_path)

    def test_is_remote_true_counts_when_is_local_is_omitted(self):
        metrics = self.parse_records([
            {"step_id": "remote-action", "outputs": ["obj/remote.o"], "is_remote": True},
        ])
        self.assertEqual(metrics["action_records"], 1)
        self.assertEqual(metrics["remote_executed_action_records"], 1)
        self.assertEqual(metrics["local_action_records"], 0)

    def test_cache_hits_and_no_exec_actions_are_not_remote_execution(self):
        metrics = self.parse_records([
            {"step_id": "remote-cache-hit", "outputs": ["obj/cached.o"],
             "is_remote": True, "cached": True},
            {"step_id": "remote-no-exec", "outputs": ["gen/handled.txt"],
             "is_remote": True, "no_exec": True},
            {"step_id": "local-action", "outputs": ["obj/local.o"], "is_local": True},
        ])
        self.assertEqual(metrics["cached_action_records"], 1)
        self.assertEqual(metrics["no_exec_action_records"], 1)
        self.assertEqual(metrics["local_action_records"], 1)
        self.assertEqual(metrics["remote_executed_action_records"], 0)

    def test_explicit_is_local_false_remains_supported(self):
        metrics = self.parse_records([
            {"step_id": "legacy-remote-action", "outputs": ["obj/legacy.o"],
             "is_local": False},
        ])
        self.assertEqual(metrics["remote_executed_action_records"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
