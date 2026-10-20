import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "ci" / "siso" / "run_compile_window.py"
SPEC = importlib.util.spec_from_file_location("sandium_compile_window", HELPER)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class SandiumCompileWindowTests(unittest.TestCase):
    def test_actual_remote_execution_signatures_match_sandfox_gate(self):
        for line in (
            'is_local=false command_id=abc',
            'step state: remote exec',
            'exec remote execute',
        ):
            with self.subTest(line=line):
                self.assertTrue(MODULE.has_remote_signature(line))

    def test_remote_backend_configuration_is_not_execution_evidence(self):
        self.assertFalse(MODULE.has_remote_signature('use reapi "remote.buildbuddy.io:443"'))

    def test_metrics_parser_distinguishes_cache_local_and_remote_execution(self):
        records = [
            {"step_id": "cache", "outputs": [], "cached": True, "is_local": False},
            {"step_id": "local", "outputs": [], "cached": False, "is_local": True},
            {"step_id": "remote", "outputs": [], "cached": False, "is_local": False},
            {"step_id": "no-exec", "outputs": [], "cached": False, "is_local": False, "no_exec": True},
            {"not_an_action": True},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.jsonl"
            path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
            metrics = MODULE.parse_metrics(path)
        self.assertEqual(metrics["records"], 5)
        self.assertEqual(metrics["action_records"], 4)
        self.assertEqual(metrics["cached_action_records"], 1)
        self.assertEqual(metrics["local_action_records"], 1)
        self.assertEqual(metrics["remote_executed_action_records"], 1)
        self.assertEqual(metrics["no_exec_action_records"], 1)

    def test_completed_ninja_frontier_matches_sandfox_format(self):
        match = MODULE.PROGRESS_LINE.match("[1515/108379] 1m58.85s F CXX obj/third_party/example.o")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "CXX obj/third_party/example.o")

    def test_gn_regeneration_marker_is_rejected(self):
        self.assertIsNotNone(MODULE.REGENERATE_NINJA.search("Regenerating ninja files"))

    def test_cached_action_records_are_not_misreported_as_remote_execution(self):
        record = {"step_id": "cached", "outputs": [], "cached": True, "is_local": False}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.jsonl"
            path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            metrics = MODULE.parse_metrics(path)
        self.assertEqual(metrics["cached_action_records"], 1)
        self.assertEqual(metrics["remote_executed_action_records"], 0)


if __name__ == "__main__":
    unittest.main()
