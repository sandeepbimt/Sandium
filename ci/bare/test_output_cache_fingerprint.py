#!/usr/bin/env python3
"""Regression tests for stable output-cache identity and legacy checkpoint migration."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FP = load_module("sandium_output_cache_fingerprint", HERE / "output_cache_fingerprint.py")
OC = load_module("sandium_output_checkpoint_for_fingerprint_test", HERE / "output_checkpoint.py")


class OutputCacheFingerprintTests(unittest.TestCase):
    def make_control_tree(self, control: Path):
        for relative in FP.INPUTS:
            path = control / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stable input: " + relative + "\n", encoding="utf-8")

    def test_fingerprint_scope_excludes_ci_orchestration(self):
        excluded = {
            "ci/bare/build_identity.py",
            "ci/bare/stage_ledger.py",
            "ci/bare/state_handoff.py",
            "ci/bare/output_checkpoint.py",
            "ci/bare/checkpoint_quality.py",
            "ci/bare/cache_fingerprint.py",
            "ci/bare/source_cache_fingerprint.py",
            "ci/bare/prepare_source.sh",
            "ci/bare/configure_siso.py",
            "ci/bare/bootstrap_chromium.sh",
            "ci/bare/package_artifact.sh",
        }
        self.assertTrue({"ci/bare/source-pin.txt", "ci/bare/gn_args.gn"}.issubset(set(FP.INPUTS)))
        self.assertTrue(excluded.isdisjoint(set(FP.INPUTS)))
        self.assertNotIn(".circleci/config.yml", FP.INPUTS)

    def test_orchestration_edits_do_not_rotate_output_cache_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory)
            self.make_control_tree(control)
            first, first_content = FP.calculate_fingerprint(control)
            (control / "ci/bare/stage_ledger.py").parent.mkdir(parents=True, exist_ok=True)
            (control / "ci/bare/stage_ledger.py").write_text("orchestration edit one")
            (control / "ci/bare/output_checkpoint.py").write_text("orchestration edit two")
            second, second_content = FP.calculate_fingerprint(control)
            self.assertEqual(first, second)
            self.assertEqual(first_content, second_content)

    def test_build_argument_change_rotates_output_cache_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory)
            self.make_control_tree(control)
            first, _ = FP.calculate_fingerprint(control)
            args = control / "ci/bare/gn_args.gn"
            args.write_text(args.read_text(encoding="utf-8") + "is_component_build = false\\n")
            second, _ = FP.calculate_fingerprint(control)
            self.assertNotEqual(first, second)

    def test_source_preparation_and_packaging_script_edits_do_not_rotate_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory)
            self.make_control_tree(control)
            excluded = (
                "ci/bare/prepare_source.sh",
                "ci/bare/configure_siso.py",
                "ci/bare/bootstrap_chromium.sh",
                "ci/bare/package_artifact.sh",
            )
            for relative in excluded:
                path = control / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original implementation\\n", encoding="utf-8")
            first, _ = FP.calculate_fingerprint(control)
            for relative in excluded:
                (control / relative).write_text("revised CI implementation\\n", encoding="utf-8")
            second, _ = FP.calculate_fingerprint(control)
            self.assertEqual(first, second)

    def test_legacy_checkpoint_allows_extra_orchestration_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory)
            self.make_control_tree(control)
            saved = OC.control_inputs(control)
            legacy_script_hashes = (
                "ci/bare/prepare_source.sh",
                "ci/bare/configure_siso.py",
                "ci/bare/bootstrap_chromium.sh",
                "ci/bare/package_artifact.sh",
            )
            for relative in legacy_script_hashes:
                path = control / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("old checkpoint input\\n", encoding="utf-8")
                saved[relative] = FP.hashlib.sha256(path.read_bytes()).hexdigest()
            OC.verify_control_inputs(saved, control)
            for relative in legacy_script_hashes:
                (control / relative).write_text("changed CI-only implementation\\n", encoding="utf-8")
            # Legacy extras are ignored; retained build inputs are still checked.
            OC.verify_control_inputs(saved, control)
            (control / "ci/bare/gn_args.gn").write_text("changed GN arguments")
            with self.assertRaises(OC.CheckpointError):
                OC.verify_control_inputs(saved, control)


if __name__ == "__main__":
    unittest.main(verbosity=2)
