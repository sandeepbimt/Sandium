"""Regression tests for Siso compile-window log initialization."""
from __future__ import annotations

import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "ci/siso/run_compile_window.py"
spec = importlib.util.spec_from_file_location("sandium_run_compile_window", MODULE_PATH)
assert spec and spec.loader
compile_window = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compile_window)


class ResetSisoInfoTests(unittest.TestCase):
    def test_every_args_attribute_is_declared_by_argument_parser(self):
        # Catch runtime-only Namespace typos such as using
        # args.remote_gate_interval_seconds when the parser declares
        # --metrics-poll-interval-seconds. py_compile cannot detect these.
        tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
        declared = set()
        referenced = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
            ):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if arg.value.startswith("--"):
                            declared.add(arg.value[2:].replace("-", "_"))
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "args"
            ):
                referenced.add(node.attr)
        self.assertEqual(
            referenced - declared,
            set(),
            "compile-window script references undeclared argparse attributes",
        )

    def test_ninja_graph_regeneration_is_allowed_but_build_gates_remain_mandatory(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("NINJA_GRAPH_REGENERATION=DETECTED; allowing graph refresh", source)
        self.assertIn("NINJA_GRAPH_REGENERATION=ALLOWED_FOR_GRAPH_REFRESH", source)
        self.assertIn("115000-task", source)
        self.assertIn("REMAINING_TASK_BUDGET_GATE=FAIL", source)
        self.assertIn("preflight_is_not_execution_proof=true", source)
        self.assertIn("INCREMENTAL_PROGRESS_GATE=FAIL", source)
        self.assertIn("ACTION_FRONTIER_CUMULATIVE_UNIQUE_LABEL_COUNT=", source)
        self.assertNotIn("NINJA_GRAPH_REGENERATION=FAIL_CLOSED", source)
        self.assertNotIn('stop_reason = "Ninja attempted to regenerate the prepared/restored graph"', source)
        self.assertNotIn("source=validated_current_workflow_preflight remote_executed_action_records=0", source)

    def test_remote_gate_accepts_only_current_run_action_specific_log_evidence_as_fallback(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("remote_signatures.add(line.strip())", source)
        self.assertIn("remote_seen = True", source)
        self.assertIn("source=current_run_siso_action_logs", source)
        self.assertIn("remote_action_signatures=0", source)
        self.assertIn("no actual remote-executed Siso actions were evidenced by metrics or current-run per-action logs", source)
        self.assertNotIn("remote_seen = True  # preflight", source)

    def test_replaces_symlink_without_truncating_its_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostics = root / "diagnostics"
            diagnostics.mkdir()
            target = root / "target.log"
            target.write_text("preserve this target", encoding="utf-8")
            info = root / "siso.INFO"
            info.symlink_to(target)

            compile_window.reset_siso_info(info, diagnostics, "stage-1")

            self.assertTrue(info.is_file())
            self.assertFalse(info.is_symlink())
            self.assertEqual(info.read_text(encoding="utf-8"), "")
            self.assertEqual(target.read_text(encoding="utf-8"), "preserve this target")

    def test_replaces_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostics = root / "diagnostics"
            diagnostics.mkdir()
            info = root / "siso.INFO"
            info.symlink_to(root / "missing-target")

            compile_window.reset_siso_info(info, diagnostics, "stage-1")

            self.assertTrue(info.is_file())
            self.assertFalse(info.is_symlink())
            self.assertEqual(info.read_text(encoding="utf-8"), "")

    def test_preserves_previous_regular_log_before_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostics = root / "diagnostics"
            diagnostics.mkdir()
            info = root / "siso.INFO"
            info.write_text("previous diagnostics", encoding="utf-8")

            compile_window.reset_siso_info(info, diagnostics, "stage-2")

            self.assertEqual(info.read_text(encoding="utf-8"), "")
            self.assertEqual(
                (diagnostics / "siso.INFO.before-stage-2").read_text(encoding="utf-8"),
                "previous diagnostics",
            )

    def test_preserves_and_replaces_directory_at_siso_info_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            diagnostics = root / "diagnostics"
            diagnostics.mkdir()
            info = root / "siso.INFO"
            info.mkdir()
            (info / "restored-note.txt").write_text("keep for diagnosis", encoding="utf-8")

            compile_window.reset_siso_info(info, diagnostics, "stage-1")

            self.assertTrue(info.is_file())
            self.assertFalse(info.is_symlink())
            self.assertEqual(info.read_text(encoding="utf-8"), "")
            preserved = diagnostics / "siso.INFO.unexpected-stage-1"
            self.assertTrue(preserved.is_dir())
            self.assertEqual(
                (preserved / "restored-note.txt").read_text(encoding="utf-8"),
                "keep for diagnosis",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
