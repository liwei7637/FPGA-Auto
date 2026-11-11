"""Source tests; host integration is excluded only when explicitly requested."""
import argparse,json,os,sys,tempfile,unittest
from pathlib import Path
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
parser=argparse.ArgumentParser()
parser.add_argument("suite",choices=("core","debug"))
parser.add_argument("--restricted-host",action="store_true",help="Explicitly exclude detached-worker and loopback integration; record exclusions")
parser.add_argument("--output",required=True,help="New directory for this run")
args=parser.parse_args()
output=Path(args.output).resolve()
output.mkdir(parents=True,exist_ok=False)
(output/"temp").mkdir()
os.environ["TEMP"]=os.environ["TMP"]=str(output/"temp")
tempfile.tempdir=None
blocked={
 "test_background.BackgroundTests.test_background_outlives_submitter_and_finishes",
 "test_background.BackgroundTests.test_background_timeout_preserves_failure_and_kills_children",
 "test_background.BackgroundTests.test_busy_project_does_not_launch_second_build",
 "test_background.BackgroundTests.test_cancel_cleans_worker_process_tree",
 "test_bitstream.BitstreamTests.test_background_preserves_build_bit_option_and_event_artifact",
 "test_monitor.MonitorTests.test_detached_monitor_survives_submitter_and_reuses_same_job",
 "test_monitor.MonitorTests.test_http_is_read_only_and_bound_to_token_and_loopback_host",
 "test_wakeup.WakeupHarnessTests.test_single_worker_waits_for_turn_end_then_finishes"
}
def flatten(suite):
 for test in suite:
  if isinstance(test,unittest.TestSuite):yield from flatten(test)
  else:yield test
if args.suite=="core":
 suite=unittest.TestLoader().discover(str(root/"tests"))
 excluded=sorted(blocked) if args.restricted_host else []
 tests=[t for t in flatten(suite) if t.id() not in excluded]
else:
 sys.path.insert(0,str(root/"debug_tests"))
 from vivado_debug.bootstrap import enable_vendor
 enable_vendor()
 if args.restricted_host:
  suite=unittest.TestLoader().loadTestsFromNames(["test_debug.DebugDataTests","test_debug.DebugModelTests"])
  excluded=["test_debug.DebugWorkerTests (detached session workers)","test_debug_mcp (SDK/transport integration; not included in this restricted unit profile)"]
 else:
  suite=unittest.TestLoader().discover(str(root/"debug_tests"));excluded=[]
 tests=list(flatten(suite))
with (output/"tests.log").open("w",encoding="utf-8") as log:
 result=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.TestSuite(tests))
record=dict(python=sys.version.split()[0],suite=args.suite,tests_run=result.testsRun,passed=result.wasSuccessful(),skipped=[dict(test=t,reason=r) for t,r in result.skipped],failures=[t.id() for t,_ in result.failures],errors=[t.id() for t,_ in result.errors],host_excluded=excluded)
(output/"result.json").write_text(json.dumps(record,indent=2),encoding="utf-8")
print(json.dumps(record))
sys.exit(0 if result.wasSuccessful() else 1)
