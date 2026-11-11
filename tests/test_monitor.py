import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fpga_auto import monitor
from fpga_auto.jobs import Job, atomic_json, read_json
from fpga_auto.portable import ensure_config, make_parser, main
from fpga_auto.results import new_result
from fpga_auto.runner import process_identity
from test_discovery import write_xpr


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fpga monitor 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = write_xpr(self.root, "design.xpr")
        self.config, _ = ensure_config(self.root, self.root / "fpga.json", str(self.project))
        self.job = Job(self.config["config_path"], "build")
        self.job.request.update(project_path=str(self.project))
        self.job.save_request()
        self.snapshot = monitor.Snapshot(self.job.directory)

    def bridge(self, **fields):
        atomic_json(self.job.directory / "bridge.json", fields)

    def finish(self, execution="completed", acceptance="passed", reason=None):
        result = new_result("build", self.job.id)
        result.update(execution=execution, acceptance=acceptance, reason=reason,
                      stage=self.job.state["stage"])
        self.job.finish(result, cancelled=reason == "cancelled")

    def test_real_phase_progress_never_increases_with_time(self):
        self.job.update(state="running", stage="synthesis")
        self.bridge(stage="synthesis", checked="true", synthesis_status="running")
        first = self.snapshot.read()
        with patch("fpga_auto.monitor.time.time", return_value=time.time() + 3600):
            later = self.snapshot.read()
        self.assertEqual(first["percent"], 20)
        self.assertEqual(later["percent"], 20)
        self.assertEqual(later["phases"][1]["status"], "active")
        self.assertGreater(later["elapsed_seconds"], first["elapsed_seconds"])

    def test_stage_progress_and_post_route_remain_monotonic(self):
        self.job.update(state="running")
        self.bridge(stage="implementation", checked="true", synthesis_status="passed")
        self.assertEqual(self.snapshot.read()["percent"], 40)
        self.bridge(stage="post_route", checked="true", synthesis_status="passed",
                    implementation_status="passed", timing_complete="true")
        self.assertEqual(self.snapshot.read()["percent"], 80)
        self.bridge(stage="post_route", checked="true", synthesis_status="passed",
                    implementation_status="passed", timing_complete="true", drc_complete="true")
        self.assertEqual(self.snapshot.read()["percent"], 99)
        self.finish()
        self.assertEqual(self.snapshot.read()["percent"], 100)

    def test_acceptance_failure_does_not_look_like_pass(self):
        self.finish(acceptance="failed")
        value = self.snapshot.read()
        self.assertEqual(value["state"], "completed")
        self.assertEqual(value["acceptance"], "failed")
        self.assertEqual(value["percent"], 100)
        self.assertFalse(value["active"])

    def test_failed_and_cancelled_jobs_do_not_claim_full_progress(self):
        self.bridge(stage="synthesis", checked="true")
        self.job.update(stage="synthesis")
        self.finish(execution="failed", acceptance="unknown", reason="cancelled")
        value = self.snapshot.read()
        self.assertEqual(value["state"], "cancelled")
        self.assertEqual(value["percent"], 20)
        self.assertEqual(value["phases"][1]["status"], "stopped")

    def test_short_operation_has_only_its_phases(self):
        self.job.update(command="synth", state="running", stage="synthesis")
        self.bridge(stage="synthesis", checked="true")
        value = self.snapshot.read()
        self.assertEqual(value["total_phases"], 2)
        self.assertEqual(value["percent"], 50)

    def test_read_live_child_logs_and_ignore_stale_logs(self):
        run = self.root / "design.runs" / "synth_1"
        run.mkdir(parents=True)
        log = run / "runme.log"
        log.write_text("综合子进程第一行\n", encoding="utf-8")
        self.job.update(state="running", stage="synthesis")
        self.bridge(stage="synthesis", synthesis_directory=str(run), synthesis_status="running")
        value = self.snapshot.read()
        self.assertIn("综合子进程第一行", value["logs"][0]["text"])
        with log.open("a", encoding="utf-8") as stream:
            stream.write("第二行\n")
        self.assertIn("第二行", self.snapshot.read()["logs"][0]["text"])
        os.utime(log, (1, 1))
        self.assertEqual(self.snapshot.read()["logs"], [])

    def test_finished_job_never_follows_later_run_logs(self):
        run = self.root / "synth_1"
        run.mkdir()
        (run / "runme.log").write_text("ANOTHER JOB", encoding="utf-8")
        self.bridge(synthesis_directory=str(run), synthesis_status="passed")
        self.finish()
        self.assertEqual(self.snapshot.read()["logs"], [])
        (self.job.directory / "reports" / "synthesis_run.log").write_text("THIS JOB", encoding="utf-8")
        self.assertEqual(self.snapshot.read()["logs"][0]["text"], "THIS JOB")

    def test_tail_read_is_bounded_and_handles_truncation_and_unicode(self):
        self.job.log.write_text("old\n" * 100000 + "最新中文\n", encoding="utf-8")
        value = self.snapshot.read()["logs"][0]
        self.assertLessEqual(len(value["text"].encode("utf-8")), monitor.TAIL_BYTES)
        self.assertTrue(value["truncated"])
        self.assertTrue(value["text"].endswith("最新中文\n"))
        self.job.log.write_text("reset\n", encoding="utf-8")
        value = self.snapshot.read()["logs"][0]
        self.assertEqual(value["text"], "reset\n")
        self.assertFalse(value["truncated"])

    def test_transient_bridge_read_does_not_reset_progress(self):
        self.bridge(stage="synthesis", checked="true")
        self.assertEqual(self.snapshot.read()["percent"], 20)
        (self.job.directory / "bridge.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.snapshot.read()["percent"], 20)

    def test_dead_owner_is_reported_as_failed(self):
        self.job.update(owner_pid=0, state="running")
        value = self.snapshot.read()
        self.assertEqual(value["state"], "failed")
        self.assertEqual(value["reason"], "owner_exited")
        with patch("fpga_auto.monitor.time.time", return_value=time.time() + 3600):
            self.assertEqual(self.snapshot.read()["elapsed_seconds"], value["elapsed_seconds"])

    def test_http_is_read_only_and_bound_to_token_and_loopback_host(self):
        self.job.log.write_text('<script>alert("unsafe")</script>\n中文日志\n', encoding="utf-8")
        with monitor.make_server(self.job.directory, "test-token") as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:" + str(server.server_port)
            try:
                with urlopen(base + "/test-token/") as response:
                    self.assertIn("实时日志", response.read().decode())
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                with urlopen(base + "/test-token/snapshot") as response:
                    self.assertIn("中文日志", json.load(response)["logs"][0]["text"])
                for request, status in ((Request(base + "/snapshot"), 404),
                                        (Request(base + "/test-token/snapshot", headers={"Host": "foreign.example"}), 403),
                                        (Request(base + "/test-token/snapshot", data=b"cancel"), 501)):
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(request)
                    self.assertEqual(raised.exception.code, status)
                    raised.exception.close()
                self.assertFalse((self.job.directory / "cancel-request.json").exists())
            finally:
                server.shutdown()
                thread.join(timeout=3)

    def test_monitor_launch_failure_is_nonfatal(self):
        with patch("fpga_auto.monitor.subprocess.Popen", side_effect=OSError("unavailable")):
            receipt = monitor.launch(self.job.directory)
        self.assertEqual(receipt["status"], "unavailable")
        self.assertEqual(read_json(self.job.directory / "state.json")["state"], "created")

    def test_latest_log_source_is_last_for_auto_scroll(self):
        self.job.log.write_text("LATEST MAIN OUTPUT", encoding="utf-8")
        archived = self.job.directory / "reports" / "implementation_run.log"
        archived.write_text("EARLIER IMPLEMENTATION", encoding="utf-8")
        os.utime(archived, (1, 1))
        self.assertEqual(self.snapshot.read()["logs"][-1]["key"], "main")

    def test_default_cli_opens_monitor_and_opt_out_is_explicit(self):
        parser = make_parser()
        self.assertFalse(parser.parse_args(["start", "synth"]).no_monitor)
        self.assertTrue(parser.parse_args(["build", "--no-monitor"]).no_monitor)
        with patch("fpga_auto.portable.resolve_vivado", return_value=Path(sys.executable)), \
                patch("fpga_auto.background.start", return_value={"execution": "submitted", "paths": {"job": str(self.job.directory)}}), \
                patch("fpga_auto.monitor.launch", return_value={"status": "ready", "url": "local-url"}) as launch, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["start", "synth", "--root", str(self.root), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["monitor"]["url"], "local-url")
        launch.assert_called_once_with(str(self.job.directory))

    def test_detached_monitor_survives_submitter_and_reuses_same_job(self):
        # The submitting Python exits before this test connects to the observer.
        portable = Path(__file__).resolve().parents[1] / "portable.py"
        response = subprocess.run([sys.executable, "-B", str(portable), "monitor", "--root", str(self.root),
                                   "--job", self.job.id, "--no-open", "--json"],
                                  capture_output=True, text=True, encoding="utf-8", timeout=12)
        self.assertEqual(response.returncode, 0, response.stderr + response.stdout)
        receipt = json.loads(response.stdout)
        self.assertEqual(receipt["status"], "ready", receipt)
        try:
            with urlopen(receipt["url"] + "snapshot", timeout=3) as response:
                self.assertEqual(json.load(response)["job_id"], self.job.id)
            again = monitor.launch(self.job.directory, open_browser=False)
            self.assertEqual(again["pid"], receipt["pid"])
            with patch("fpga_auto.monitor.webbrowser.open", side_effect=OSError("no browser")):
                unavailable = monitor.launch(self.job.directory)
            self.assertEqual(unavailable["status"], "ready")
            self.assertFalse(unavailable["browser_opened"])
        finally:
            if process_identity(receipt["pid"]) == receipt["identity"]:
                os.kill(receipt["pid"], signal.SIGTERM)
                deadline = time.monotonic() + 5
                while process_identity(receipt["pid"]) and time.monotonic() < deadline:
                    time.sleep(.05)
        # Closing the observer has no effect on the build owner's state.
        self.assertEqual(read_json(self.job.directory / "state.json")["state"], "created")
        self.assertIsNotNone(process_identity(os.getpid()))

    def test_unobserved_server_exits_without_affecting_job(self):
        script = Path(__file__).resolve().parents[1] / "monitor.py"
        response = subprocess.run([sys.executable, "-B", str(script), str(self.job.directory), "--idle-timeout", ".05"],
                                  capture_output=True, text=True, timeout=5)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(read_json(self.job.directory / "state.json")["state"], "created")


if __name__ == "__main__":
    unittest.main()
