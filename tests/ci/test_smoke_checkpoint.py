"""Tests for the lightweight five-stage smoke checkpoint chain."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ci/smoke/checkpoint.py"


class SmokeCheckpointTests(unittest.TestCase):
    def test_five_stage_chain_passes_without_creating_production_apk(self):
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "smoke-state"
            env = os.environ.copy()
            env["CIRCLE_WORKFLOW_ID"] = "test-workflow"
            env["CIRCLE_SHA1"] = "test-commit"
            for stage in range(1, 6):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), str(stage), str(stage - 1), "--root", str(state_root)],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            records = [json.loads((state_root / f"stage-{stage}.json").read_text()) for stage in range(1, 6)]
            self.assertTrue(all(item["mode"] == "smoke" for item in records))
            self.assertTrue(all(item["production_apk_created"] is False for item in records))
            self.assertIn("SANDIUM_SMOKE_FIVE_STAGE_HANDOVER=PASS", result.stdout)

    def test_stage_rejects_checkpoint_from_different_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            state_root = Path(directory) / "smoke-state"
            env = os.environ.copy()
            env["CIRCLE_WORKFLOW_ID"] = "workflow-one"
            env["CIRCLE_SHA1"] = "test-commit"
            first = subprocess.run(
                [sys.executable, str(SCRIPT), "1", "0", "--root", str(state_root)],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            env["CIRCLE_WORKFLOW_ID"] = "workflow-two"
            second = subprocess.run(
                [sys.executable, str(SCRIPT), "2", "1", "--root", str(state_root)],
                cwd=ROOT, env=env, text=True, capture_output=True,
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("different stage, workflow, or commit", second.stderr)


if __name__ == "__main__":
    unittest.main()
