#!/usr/bin/env python3
"""Unit tests for Siso evidence parsing and incremental progress gates."""
import importlib.util, json, tempfile, unittest
from pathlib import Path
HERE=Path(__file__).resolve().parent
SPEC=importlib.util.spec_from_file_location("sandium_stage_ledger",HERE/"stage_ledger.py")
assert SPEC and SPEC.loader
M=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(M)
class LedgerTests(unittest.TestCase):
 def test_cached_record_without_locality_is_cache_reuse_only(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d); (p/"m").write_text(json.dumps({"step_id":"x","outputs":["a.o"],"cached":True})+"\n")
   (p/"l").write_text('use reapi "remote.buildbuddy.io:443"\n')
   m=M.parse_metrics(p/"m",p/"l")
   self.assertTrue(m["remote_action_cache_reuse_proven"]); self.assertFalse(m["remote_execution_proven"])
 def test_endpoint_alone_is_not_remote_execution(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d); (p/"l").write_text('use reapi "remote.buildbuddy.io:443"\n')
   self.assertFalse(M.parse_metrics(p/"missing",p/"l")["remote_execution_proven"])
 def test_siso_summary_counts_are_parsed(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d); (p/"l").write_text("Siso finished local:8, remote:81, cache:103\n")
   x=M.parse_metrics(p/"missing",p/"l")
   self.assertEqual(x["summary_remote_actions"],81); self.assertEqual(x["summary_cache_hits"],103)
 def test_stage_one_requires_progress_and_remote_or_cache_evidence(self):
  from build_identity import IdentityError
  with self.assertRaises(IdentityError): M.validate_progress(1,{"remote_execution_proven":False,"remote_action_cache_reuse_proven":False},1,10,None)
 def test_later_stage_requires_forward_progress(self):
  from build_identity import IdentityError
  prev={"max_completed_actions":10,"total_action_count":100}
  with self.assertRaises(IdentityError): M.validate_progress(2,{"remote_execution_proven":True},10,100,prev)
  self.assertEqual(M.validate_progress(2,{"remote_action_cache_reuse_proven":True},20,100,prev),"REMOTE_ACTION_CACHE_HIT_WITH_FORWARD_PROGRESS")
 def test_minor_graph_total_variance_from_manifest_refresh_is_tolerated(self):
  prior={"max_completed_actions":53323,"total_action_count":109081}
  metrics={"remote_action_cache_reuse_proven":True}
  self.assertEqual(
   M.validate_progress(2,metrics,54738,109087,prior),
   "REMOTE_ACTION_CACHE_HIT_WITH_FORWARD_PROGRESS",
  )

 def test_material_graph_total_drift_fails(self):
  from build_identity import IdentityError
  previous={"max_completed_actions":10,"total_action_count":100}
  with self.assertRaisesRegex(IdentityError,"beyond 1% tolerance"):
   M.validate_progress(2,{"remote_execution_proven":True},20,105,previous)
if __name__=="__main__": unittest.main(verbosity=2)
