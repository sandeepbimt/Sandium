#!/usr/bin/env python3
"""Tests for early verification of restored compiled object reuse."""
import hashlib, importlib.util, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
SPEC=importlib.util.spec_from_file_location("reuse_preflight",Path(__file__).with_name("compiled_output_reuse_preflight.py"))
assert SPEC and SPEC.loader
M=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

def write_inputs(source):
    siso=source/"build/config/siso"; siso.mkdir(parents=True)
    result={}
    for name,data in ((".sisoenv",b"SISO_ENV=stable\n"),(".sisorc",b"ninja -re_cache_enable_write=true\n")):
        path=siso/name; path.write_bytes(data); result[name]=hashlib.sha256(data).hexdigest()
    return result
def fixture(root):
    source=root/"src"; write_inputs(source)
    out=root/M.OUTPUT_RELATIVE; out.mkdir(parents=True)
    (out/"build.ninja").write_text("stable graph\n"); (out/"build.ninja.stamp").write_text("stable stamp\n"); (out/"args.gn").write_text("use_siso=true\n")
    samples=[]; obj=out/"obj/test"; obj.mkdir(parents=True)
    for i in range(8):
        target=f"obj/test/unit_{i}.o"; path=out/target; data=f"object-{i}".encode(); path.write_bytes(data)
        samples.append({"path":target,"sha256":hashlib.sha256(data).hexdigest(),"bytes":len(data)})
    report=root/"sandium-state-handoff-report.json"
    report.write_text(json.dumps({"local_compiled_outputs_restored":True,"compiled_output_action_samples":samples}))
    return source,out,report
class Tests(unittest.TestCase):
    def test_normalizes_input_timestamps_without_byte_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp); hashes=write_inputs(source)
            self.assertEqual(M.normalize_siso_input_timestamps(source,946684800),hashes)
            for name in M.SISO_INPUT_NAMES:
                path=source/"build/config/siso"/name
                self.assertEqual(int(path.stat().st_mtime),946684800); self.assertEqual(M.sha256_file(path),hashes[name])
    def test_no_work_preflight_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source,out,report=fixture(root)
            fake=subprocess.CompletedProcess(["autoninja"],0,"ninja: no work to do.\n",None)
            with patch.object(M.shutil,"which",return_value="/toolchain/autoninja"),patch.object(M.subprocess,"run",return_value=fake) as run:
                result=M.verify_restored_output_reuse(root=root,source_root=source,stage=2,report_path=report,epoch=946684800)
            self.assertEqual(result["status"],"PASS"); self.assertEqual(result["sample_count"],8)
            self.assertIn("obj/test/unit_0.o",run.call_args.args[0])
    def test_legacy_aggregate_samples_are_filtered_before_reuse_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source, out, report = fixture(root)
            handoff = json.loads(report.read_text(encoding="utf-8"))
            handoff["compiled_output_action_samples"].append({
                "path": "obj/test/libaggregate.a",
                "sha256": "a" * 64,
                "bytes": 123,
            })
            report.write_text(json.dumps(handoff), encoding="utf-8")
            fake = subprocess.CompletedProcess(["autoninja"], 0, "ninja: no work to do.\n", None)
            with patch.object(M.shutil, "which", return_value="/toolchain/autoninja"), \
                 patch.object(M.subprocess, "run", return_value=fake) as run:
                result = M.verify_restored_output_reuse(
                    root=root, source_root=source, stage=2, report_path=report, epoch=946684800
                )
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["sample_count"], 8)
            self.assertNotIn("obj/test/libaggregate.a", run.call_args.args[0])

    def test_legacy_aggregate_samples_cannot_substitute_for_leaf_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source, out, report = fixture(root)
            handoff = json.loads(report.read_text(encoding="utf-8"))
            handoff["compiled_output_action_samples"] = handoff["compiled_output_action_samples"][:7] + [
                {"path": "obj/test/libaggregate.a", "sha256": "a" * 64, "bytes": 123}
            ]
            report.write_text(json.dumps(handoff), encoding="utf-8")
            fake = subprocess.CompletedProcess(["autoninja"], 0, "ninja: no work to do.\n", None)
            with patch.object(M.shutil, "which", return_value="/toolchain/autoninja"), \
                 patch.object(M.subprocess, "run", return_value=fake):
                with self.assertRaisesRegex(M.ReusePreflightError, "too few verified leaf-object"):
                    M.verify_restored_output_reuse(
                        root=root, source_root=source, stage=2, report_path=report, epoch=946684800
                    )

    def test_siso_dry_run_invocation_is_not_mistaken_for_scheduled_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source,out,report=fixture(root)
            handoff=json.loads(report.read_text(encoding="utf-8"))
            targets=[item["path"] for item in handoff["compiled_output_action_samples"]]
            # Reproduce the observed Siso wrapper line: every requested target is
            # present as an argument, but no compile action is listed.
            echoed=(f"{targets[0]}: depot_tools/siso.py: ninja "
                    f"-re_cache_enable_write=true -n -C {out} " + " ".join(targets) + "\n")
            fake=subprocess.CompletedProcess(["autoninja"],0,echoed,None)
            with patch.object(M.shutil,"which",return_value="/toolchain/autoninja"),patch.object(M.subprocess,"run",return_value=fake):
                with self.assertRaisesRegex(M.ReusePreflightError,"no explicit no-work marker"):
                    M.verify_restored_output_reuse(root=root,source_root=source,stage=2,report_path=report,epoch=946684800)

    def test_unrecognized_dry_run_summary_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source,out,report=fixture(root)
            fake=subprocess.CompletedProcess(["autoninja"],0,"ninja: Entering directory 'out'\\nSiso finished successfully\\n",None)
            with patch.object(M.shutil,"which",return_value="/toolchain/autoninja"),patch.object(M.subprocess,"run",return_value=fake):
                with self.assertRaisesRegex(M.ReusePreflightError,"no explicit no-work marker"):
                    M.verify_restored_output_reuse(root=root,source_root=source,stage=2,report_path=report,epoch=946684800)

    def test_dirty_sample_fails_before_expensive_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source,out,report=fixture(root)
            fake=subprocess.CompletedProcess(["autoninja"],0,"clang++ -o obj/test/unit_0.o input.cc\n",None)
            with patch.object(M.shutil,"which",return_value="/toolchain/autoninja"),patch.object(M.subprocess,"run",return_value=fake):
                with self.assertRaisesRegex(M.ReusePreflightError,"scheduled for rebuild"):
                    M.verify_restored_output_reuse(root=root,source_root=source,stage=2,report_path=report,epoch=946684800)
    def test_graph_change_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source,out,report=fixture(root)
            def mutate(*args,**kwargs):
                (out/"build.ninja").write_text("changed graph\n")
                return subprocess.CompletedProcess(["autoninja"],0,"ninja: no work to do.\n",None)
            with patch.object(M.shutil,"which",return_value="/toolchain/autoninja"),patch.object(M.subprocess,"run",side_effect=mutate):
                with self.assertRaisesRegex(M.ReusePreflightError,"build.ninja changed"):
                    M.verify_restored_output_reuse(root=root,source_root=source,stage=2,report_path=report,epoch=946684800)
    def test_stamp_mtime_only_change_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source,out,report=fixture(root)
            def touch_stamp(*args,**kwargs):
                stamp=out/"build.ninja.stamp"
                before=stamp.stat()
                os.utime(stamp,ns=(before.st_atime_ns,before.st_mtime_ns+2_000_000_000))
                return subprocess.CompletedProcess(["autoninja"],0,"ninja: no work to do.\\n",None)
            with patch.object(M.shutil,"which",return_value="/toolchain/autoninja"),patch.object(M.subprocess,"run",side_effect=touch_stamp):
                with self.assertRaisesRegex(M.ReusePreflightError,"build.ninja.stamp changed"):
                    M.verify_restored_output_reuse(root=root,source_root=source,stage=2,report_path=report,epoch=946684800)
    def test_stage_one_without_restored_outputs_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source, out, report = fixture(root)
            report.unlink()
            with patch.object(M.shutil, "which", return_value="/toolchain/autoninja"), \
                 patch.object(M.subprocess, "run") as run:
                with self.assertRaisesRegex(M.ReusePreflightError, "No validated restored-output handoff"):
                    M.verify_restored_output_reuse(root=root, source_root=source, stage=1,
                                                   report_path=report, epoch=946684800)
            run.assert_not_called()

    def test_stage_one_without_verified_object_samples_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source, out, report = fixture(root)
            report.write_text(json.dumps({
                "local_compiled_outputs_restored": True, "compiled_output_action_samples": []
            }))
            with patch.object(M.shutil, "which", return_value="/toolchain/autoninja"), \
                 patch.object(M.subprocess, "run") as run:
                with self.assertRaisesRegex(M.ReusePreflightError, "too few verified leaf-object action samples"):
                    M.verify_restored_output_reuse(root=root, source_root=source, stage=1,
                                                   report_path=report, epoch=946684800)
            run.assert_not_called()

    def test_stage_runner_uses_structured_result_not_driver_log(self):
        runner = Path(M.__file__).with_name("run_stage.sh").read_text(encoding="utf-8")
        self.assertIn('--result-report "$REUSE_REPORT"', runner)
        self.assertNotIn('REUSE_GRAPH_SHA="$(sed -n', runner)

    def test_continuation_missing_restored_checkpoint_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/"src"; write_inputs(source)
            (root/M.OUTPUT_RELATIVE).mkdir(parents=True)
            (root/M.OUTPUT_RELATIVE/"build.ninja").write_text("graph\n")
            (root/M.OUTPUT_RELATIVE/"build.ninja.stamp").write_text("stamp\n")
            report=root/"sandium-state-handoff-report.json"
            report.write_text(json.dumps({"local_compiled_outputs_restored":False,"compiled_output_action_samples":[]}))
            with self.assertRaisesRegex(M.ReusePreflightError,"[Nn]o restored compiled-output checkpoint"):
                M.verify_restored_output_reuse(root=root,source_root=source,stage=2,report_path=report,epoch=946684800)
    def test_symlinked_or_missing_siso_config_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); siso=root/"build/config/siso"; siso.mkdir(parents=True)
            (siso/".sisoenv").write_text("x\n"); (siso/".sisorc").symlink_to(siso/".sisoenv")
            with self.assertRaisesRegex(M.ReusePreflightError,"missing/unsafe"):
                M.normalize_siso_input_timestamps(root,946684800)
if __name__=="__main__":
    unittest.main(verbosity=2)
