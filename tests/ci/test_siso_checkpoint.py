"""Tests for SANDIUM's resumable, compressed Android build checkpoints."""
from __future__ import annotations
import hashlib, json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ci/siso/checkpoint.py"


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("zstd") or not shutil.which("tar"):
            self.skipTest("tar and zstd are required")
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.output = self.root / "src/out/android_Debug_arm64"
        self.output.mkdir(parents=True)
        state = {
            "schema": 1, "source_commit": "a" * 40, "brave_core_commit": "a" * 40,
            "chromium_commit": "b" * 40, "chromium_version": "151.0.7922.38",
            "target": "android-arm64-debug-apk", "build_config": "Debug",
            "output_dir": "src/out/android_Debug_arm64",
            "build_input_fingerprint": "c" * 64, "gn_args_sha256": "d" * 64,
            "compile_complete": False, "artifact_verified": False,
        }
        (self.output / ".sandium-build-state.json").write_text(json.dumps(state), encoding="utf-8")
        (self.output / "args.gn").write_text("use_siso = true\nuse_reclient = false\n", encoding="utf-8")
        (self.output / "build.ninja").write_text("# test graph\n", encoding="utf-8")
        (self.output / ".siso_fs_state").write_bytes(b"siso-state")
        (self.output / ".siso_fs_state.journal").write_bytes(b"siso-journal")
        obj = self.output / "obj/sample/sample.o"
        obj.parent.mkdir(parents=True)
        obj.write_bytes(b"object payload")
        os.utime(obj, ns=(1767247445123456789, 1767247445123456789))

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, action, *args):
        return subprocess.run([sys.executable, str(SCRIPT), action, "--root", str(self.root), *args],
                              cwd=ROOT, text=True, capture_output=True)

    def test_round_trip_preserves_siso_state_graph_and_nanosecond_mtime(self):
        obj = self.output / "obj/sample/sample.o"
        expected_mtime = obj.stat().st_mtime_ns
        digest = hashlib.sha256(obj.read_bytes()).hexdigest()
        packed = self.invoke("pack", "--stage", "stage-1")
        self.assertEqual(packed.returncode, 0, packed.stdout + packed.stderr)
        self.assertIn("SANDIUM_CHECKPOINT_PACK=PASS", packed.stdout)
        shutil.rmtree(self.output)
        restored = self.invoke("restore")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        self.assertIn("SANDIUM_CHECKPOINT_RESTORE=PASS", restored.stdout)
        actual = self.output / "obj/sample/sample.o"
        self.assertEqual(actual.stat().st_mtime_ns, expected_mtime)
        self.assertEqual(hashlib.sha256(actual.read_bytes()).hexdigest(), digest)
        self.assertTrue((self.output / ".siso_fs_state").is_file())
        self.assertTrue((self.output / ".siso_fs_state.journal").is_file())
        self.assertTrue((self.output / "build.ninja").is_file())

    def test_missing_checkpoint_preserves_legacy_raw_output(self):
        sentinel = self.output / "legacy.txt"
        sentinel.write_text("keep", encoding="utf-8")
        result = self.invoke("restore")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SANDIUM_CHECKPOINT_RESTORE=MISS", result.stdout)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_corrupt_checkpoint_does_not_delete_existing_output(self):
        packed = self.invoke("pack", "--stage", "stage-1")
        self.assertEqual(packed.returncode, 0, packed.stdout + packed.stderr)
        sentinel = self.output / "preserve.txt"
        sentinel.write_text("existing output", encoding="utf-8")
        archive = self.root / "sandium-checkpoint.tar.zst"
        raw = bytearray(archive.read_bytes())
        raw[len(raw) // 2] ^= 1
        archive.write_bytes(raw)
        result = self.invoke("restore")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SANDIUM_CHECKPOINT_RESTORE=REJECTED", result.stdout)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "existing output")

    def test_pack_rejects_missing_siso_state_or_journal(self):
        for missing in (".siso_fs_state", ".siso_fs_state.journal"):
            with self.subTest(missing=missing):
                path = self.output / missing
                saved = path.read_bytes()
                path.unlink()
                result = self.invoke("pack", "--stage", "stage-1")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Required checkpoint input is missing or empty", result.stderr)
                path.write_bytes(saved)

    def test_pack_rejects_bad_chromium_version(self):
        path = self.output / ".sandium-build-state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["chromium_version"] = "not-a-version"
        path.write_text(json.dumps(state), encoding="utf-8")
        result = self.invoke("pack", "--stage", "stage-1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid Chromium version", result.stderr)


if __name__ == "__main__":
    unittest.main()
