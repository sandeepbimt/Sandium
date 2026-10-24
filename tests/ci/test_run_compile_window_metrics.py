"""Functional tests for the compile-window Siso remote-execution gate."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci" / "siso"))
import run_compile_window as compile_window  # noqa: E402


class ParseMetricsTests(unittest.TestCase):
    def parse_records(self, records: list[object]) -> dict[str, int]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.jsonl"
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            return compile_window.parse_metrics(path)

    @staticmethod
    def action(**fields: object) -> dict[str, object]:
        record: dict[str, object] = {
            "step_id": "step-1",
            "outputs": ["out/obj/example.o"],
        }
        record.update(fields)
        return record

    def test_uncached_explicit_remote_action_counts_as_execution_proof(self) -> None:
        result = self.parse_records([self.action(is_remote=True)])
        self.assertEqual(result["remote_executed_action_records"], 1)

    def test_cached_remote_action_does_not_count_as_execution(self) -> None:
        result = self.parse_records([self.action(is_remote=True, cached=True)])
        self.assertEqual(result["remote_executed_action_records"], 0)
        self.assertEqual(result["cached_action_records"], 1)

    def test_remote_no_exec_record_does_not_count_as_execution(self) -> None:
        result = self.parse_records([self.action(is_remote=True, no_exec=True)])
        self.assertEqual(result["remote_executed_action_records"], 0)
        self.assertEqual(result["no_exec_action_records"], 1)

    def test_explicit_nonlocal_uncached_action_counts_as_remote(self) -> None:
        result = self.parse_records([self.action(is_local=False)])
        self.assertEqual(result["remote_executed_action_records"], 1)

    def test_remote_cache_counts_as_reported_remote_but_not_execution(self) -> None:
        result = self.parse_records([self.action(is_remote=True, cached=True)])
        self.assertEqual(result["explicit_remote_action_records"], 1)
        self.assertEqual(result["remote_cached_action_records"], 1)
        self.assertEqual(result["remote_executed_action_records"], 0)

    def test_local_action_does_not_count_as_remote(self) -> None:
        result = self.parse_records([self.action(is_local=True)])
        self.assertEqual(result["remote_executed_action_records"], 0)
        self.assertEqual(result["local_action_records"], 1)

    def test_non_action_diagnostics_do_not_count_as_execution(self) -> None:
        result = self.parse_records([
            {"message": "is_remote=true"},
            {"step_id": "step-without-outputs"},
            {"outputs": []},
        ])
        self.assertEqual(result["records"], 3)
        self.assertEqual(result["action_records"], 0)
        self.assertEqual(result["remote_executed_action_records"], 0)

    def test_invalid_jsonl_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.jsonl"
            path.write_text('{"step_id": "ok", "outputs": []}\nnot-json\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid Siso metrics JSONL line 2"):
                compile_window.parse_metrics(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
