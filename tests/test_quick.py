"""Offline quick regression. Does not run Vivado or contact hardware."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from fpga_auto import cli, portable, monitor, jobs
from fpga_auto.results import new_result, apply_acceptance, exit_code
from fpga_auto.quick import parse_quick_setup, REQUIRED_FILES

def timing(wns="0.125",tns="0.000",fails="0",total="42",version="2020.2"):
    return ("Tool Version : Vivado v%s\nDesign Timing Summary\n"
            "WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints\n"
            "------- ------- ------- -------\n%s %s %s %s\nClock Summary\n") % (version,wns,tns,fails,total)

class QuickTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="fpga quick offline ")
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.reports=self.root/"reports"
        self.q=self.reports/"quick_check"
        self.q.mkdir(parents=True)
        for name in REQUIRED_FILES:(self.q/name).write_text("fixture\n",encoding="utf-8")
        for name in ("post_synth_timing.rpt","post_place_timing.rpt"):
            (self.q/name).write_text(timing(),encoding="utf-8")
        (self.root/"vivado.log").write_text("main log\n",encoding="utf-8")
        for name in ("synthesis_run.log","quick_run.log"):
            (self.reports/name).write_text("run complete\n",encoding="utf-8")
    def result(self):
        r=new_result("quick")
        r.update(execution="completed")
        r["synthesis"]["status"]="passed"
        apply_acceptance(r,self.reports)
        return r
    def test_shared_quick_cli_and_start(self):
        self.assertEqual(cli.make_parser().parse_args(["quick"]).command,"quick")
        self.assertEqual(portable.make_parser().parse_args(["quick"]).command,"quick")
        a=portable.make_parser().parse_args(["start","quick"])
        self.assertEqual(a.operation,"quick")
    def test_quick_rejects_bit_option(self):
        with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            cli.make_parser().parse_args(["quick","--bit"])
    def test_success_is_only_quick_scope(self):
        r=self.result()
        self.assertEqual((r["acceptance"],r["acceptance_scope"],exit_code(r)),("passed","quick_pre_route",0))
        self.assertEqual(r["implementation"]["status"],"unknown")
        self.assertEqual(r["timing"]["status"],"unknown")
        self.assertEqual(r["drc"]["status"],"unknown")
        self.assertEqual(r["bitstream"]["status"],"not_requested")
    def test_post_place_setup_failure(self):
        (self.q/"post_place_timing.rpt").write_text(timing("-0.2","-1.2","2"))
        r=self.result()
        self.assertEqual(r["acceptance"],"failed")
        self.assertEqual(exit_code(r),2)
    def test_missing_or_empty_each_evidence_fails_closed(self):
        for name in REQUIRED_FILES:
            p=self.q/name;data=p.read_bytes();p.write_bytes(b"")
            with self.subTest(name=name):self.assertEqual(self.result()["acceptance"],"unknown")
            p.write_bytes(data)
    def test_known_violation_not_masked_by_missing_evidence(self):
        (self.q/"post_place_timing.rpt").write_text(timing("-0.2","-1.2","2"))
        (self.q/"post_place.dcp").unlink()
        self.assertEqual(self.result()["acceptance"],"failed")
    def test_missing_log_unknown(self):
        (self.reports/"synthesis_run.log").unlink()
        r=self.result()
        self.assertEqual((r["acceptance"],exit_code(r)),("unknown",3))
    def test_constraint_diagnostics_rejected(self):
        for code in ("Designutils 20-1307","Common 17-1548","Constraints 18-513","Constraints 18-515"):
            (self.root/"vivado.log").write_text("CRITICAL WARNING: ["+code+"] invalid constraint\n")
            with self.subTest(code=code):self.assertEqual(self.result()["acceptance"],"failed")
    def test_native_child_constraint_errors_rejected(self):
        native=(Path(__file__).resolve().parents[1]/"tcl/quick_common.tcl").exists()
        if not native:self.skipTest("MIG keeps its in-process project script")
        (self.reports/"quick_run.log").write_text("ERROR: [Common 17-1548] failed XDC\n")
        self.assertEqual(self.result()["acceptance"],"failed")
        (self.reports/"quick_run.log").unlink()
        self.assertEqual(self.result()["acceptance"],"unknown")
    def test_malformed_numeric_and_version_reports_unknown(self):
        cases=[timing(version="2018.3"),timing(total="0"),timing(wns="nan"),
               timing(tns="1"),timing(fails="43"),"not a report",timing(wns="inf")]
        for text in cases:
            with self.subTest(text=text):self.assertEqual(parse_quick_setup(text)["status"],"unknown")
    def test_synthesis_failure_cannot_pass(self):
        r=new_result("quick");r["synthesis"]["status"]="failed"
        apply_acceptance(r,self.reports)
        self.assertEqual(r["acceptance"],"failed")
    def test_post_synth_risk_does_not_replace_post_place(self):
        (self.q/"post_synth_timing.rpt").write_text(timing("-1","-10","4"))
        r=self.result()
        self.assertEqual(r["acceptance"],"passed")
        self.assertEqual(r["quick_screening"]["post_synth"]["status"],"failed")
    def test_monitor_has_five_distinct_quick_phases(self):
        self.assertEqual(len(monitor.PHASES["quick"]),5)
        self.assertNotIn("implementation",[k for k,_ in monitor.PHASES["quick"]])
    def test_other_command_scopes_unchanged(self):
        for command in ("build","bit","timing","drc"):
            self.assertEqual(new_result(command)["acceptance_scope"],command)
    def test_v2_bridge_allows_quick_when_present(self):
        if hasattr(jobs,"BRIDGE_OPERATIONS"):
            self.assertIn("quick",jobs.BRIDGE_OPERATIONS)

    def test_execute_quick_uses_normal_job_lock_and_never_bit(self):
        import json,shutil
        from unittest.mock import patch
        config=self.root/"fpga.json"
        (self.root/"design.xpr").write_text("<Project/>")
        config.write_text(json.dumps(dict(project="design.xpr",vivado_version="2020.2",
                                         synth_run="synth_1",impl_run="impl_1",jobs=1)))
        called=[]
        def fake_run(executable,script,cfg,job,lock,timeout):
            called.append(script.name)
            self.assertIsNotNone(lock)
            shutil.copytree(self.q,job.directory/"reports/quick_check")
            job.log.write_text("clean main log")
            for name in ("synthesis_run.log","quick_run.log"):
                (job.directory/"reports"/name).write_text("clean run log")
            return 0,dict(done="true",stage="post_place_screening",synthesis_status="passed",quick_complete="true")
        args=cli.make_parser().parse_args(["quick","--config",str(config),"--no-monitor"])
        with patch("fpga_auto.cli.find_vivado",return_value=Path("fake.exe")), \
             patch("fpga_auto.cli.verify_version"),patch("fpga_auto.cli.run_vivado",side_effect=fake_run), \
             patch("fpga_auto.bitstream.reserve_output",side_effect=AssertionError("BIT must not run")):
            result=cli.execute(args)
        self.assertEqual(called,["quick.tcl"])
        self.assertEqual(result["execution"],"completed")
        self.assertEqual(result["acceptance"],"passed")
        self.assertEqual(result["bitstream"]["status"],"not_requested")

if __name__=="__main__":unittest.main()
