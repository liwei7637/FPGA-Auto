import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fpga_auto import background, cli, monitor, portable, runner
from fpga_auto.jobs import Job, ProjectLock, atomic_json, read_json
from fpga_auto.results import FPGAError, exit_code
from fake_bit_vivado import TIMING, DRC
from test_discovery import write_xpr


class BitstreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fpga bit 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = write_xpr(self.root, "test.xpr")
        self.config, _ = portable.ensure_config(self.root, self.root / "fpga.json", str(self.project))
        self.job = None
        self.calls = []
        self.report_timing = TIMING
        self.report_drc = DRC
        self.write_mode = "normal"

    def fake_run(self, executable, script, config, job, lock, timeout):
        self.calls.append((script.stem, job.id, id(lock), timeout))
        bridge = dict(done="true", stage="post_route", checked="true", synthesis_status="passed",
                      implementation_status="passed", timing_complete="true", drc_complete="true")
        if script.stem != "write_bit":
            (job.directory / "reports/timing_summary.rpt").write_text(self.report_timing)
            (job.directory / "reports/drc.rpt").write_text(self.report_drc)
            if self.write_mode == "cancel_before":
                atomic_json(job.directory / "cancel-request.json", {})
            if self.write_mode == "existing":
                (job.directory / "artifacts").mkdir()
                (job.directory / "artifacts/design.bit").write_bytes(b"old")
            if self.write_mode == "stale":
                bridge.update(done="false", reason="implementation_result_stale")
                return 1, bridge
        else:
            with self.assertRaises(FPGAError) as busy:
                with ProjectLock(self.project, job):
                    pass
            self.assertEqual(busy.exception.reason, "project_busy")
            bridge.update(stage="bitstream", bitstream_status="passed", bitstream_path=job.request["bitstream_output"])
            output = Path(bridge["bitstream_path"])
            if self.write_mode not in ("missing", "cancel", "timeout"):
                output.write_bytes(b"" if self.write_mode == "empty" else b"new test bitstream")
            if self.write_mode in ("cancel", "timeout"):
                raise FPGAError("cancelled" if self.write_mode == "cancel" else "timeout", "test interruption", "bitstream")
            if self.write_mode == "wrong_path":
                bridge["bitstream_path"] = str(self.root / "old.bit")
            if self.write_mode == "failed":
                bridge.update(done="false", bitstream_status="failed", reason="bitstream_failed")
                atomic_json(job.directory / "bridge.json", bridge)
                return 1, bridge
        atomic_json(job.directory / "bridge.json", bridge)
        return 0, bridge

    def execute(self, command="bit", flag=False, timeout=30):
        self.job = Job(self.config["config_path"], command)
        args = argparse.Namespace(command=command, config=str(self.config["config_path"]),
                                  vivado=sys.executable, timeout=timeout, bit=flag, no_monitor=True)
        with patch("fpga_auto.cli.verify_version"), patch("fpga_auto.cli.run_vivado", side_effect=self.fake_run):
            return cli.execute(args, self.job)

    def test_bit_uses_one_job_lock_and_publishes_hashed_artifact(self):
        result = self.execute()
        self.assertEqual(exit_code(result), 0, result)
        self.assertEqual([x[0] for x in self.calls], ["bit", "write_bit"])
        self.assertEqual(self.calls[0][1:3], self.calls[1][1:3])
        self.assertLessEqual(self.calls[1][3], self.calls[0][3])
        artifact = result["bitstream"]
        self.assertEqual(hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest(), artifact["sha256"])
        self.assertEqual(read_json(Path(artifact["path"]).parent / "bitstream.json")["job_id"], result["job_id"])

    def test_build_flag_adds_bit_but_plain_build_is_unchanged(self):
        self.assertEqual(self.execute("build")["bitstream"]["status"], "not_requested")
        self.assertEqual([x[0] for x in self.calls], ["build"])
        self.calls.clear()
        self.assertEqual(exit_code(self.execute("build", True)), 0)
        self.assertEqual([x[0] for x in self.calls], ["build", "write_bit"])

    def test_timing_failure_blocks_bit_with_exit_two(self):
        self.report_timing = TIMING.replace("1.0 0.0 0 8", "-1.0 -1.0 1 8", 1)
        result = self.execute()
        self.assertEqual(exit_code(result), 2)
        self.assertEqual(result["bitstream"]["status"], "skipped")
        self.assertEqual(len(self.calls), 1)

    def test_unknown_timing_blocks_bit_with_exit_three(self):
        self.report_timing = "incomplete report"
        result = self.execute()
        self.assertEqual(exit_code(result), 3)
        self.assertEqual(len(self.calls), 1)

    def test_drc_failure_blocks_bit(self):
        self.report_drc = DRC.replace("No DRC violations found", "Violations found: 1\n| UCIO-1 | Error | Missing pin | 1 |")
        self.assertEqual(exit_code(self.execute()), 2)
        self.assertEqual(len(self.calls), 1)

    def test_stale_implementation_is_not_rebuilt(self):
        self.write_mode = "stale"
        result = self.execute()
        self.assertEqual(result["reason"], "implementation_result_stale")
        self.assertEqual(len(self.calls), 1)

    def test_existing_job_artifact_is_not_reused_or_overwritten(self):
        self.write_mode = "existing"
        result = self.execute()
        self.assertEqual(result["reason"], "bitstream_output_exists")
        self.assertEqual((self.job.directory / "artifacts/design.bit").read_bytes(), b"old")
        self.assertEqual(len(self.calls), 1)

    def test_missing_empty_and_mismatched_artifacts_fail(self):
        for mode in ("missing", "empty", "wrong_path", "failed"):
            with self.subTest(mode=mode):
                self.write_mode = mode
                result = self.execute()
                self.assertEqual(exit_code(result), 1)
                self.assertEqual(result["bitstream"]["status"], "failed")
                self.assertNotIn("bitstream", result["paths"])

    def test_cancel_between_phases_never_starts_bit(self):
        self.write_mode = "cancel_before"
        self.assertEqual(exit_code(self.execute()), 130)
        self.assertEqual(len(self.calls), 1)

    def test_cancel_and_timeout_in_bit_stage(self):
        for mode, expected in (("cancel", 130), ("timeout", 1)):
            with self.subTest(mode=mode):
                self.write_mode = mode
                result = self.execute()
                self.assertEqual(exit_code(result), expected)
                self.assertEqual(result["stage"], "bitstream")

    def test_total_timeout_is_not_restarted_for_bit(self):
        with patch("fpga_auto.cli.time.monotonic", side_effect=[0, 0, 10]):
            result = self.execute(timeout=5)
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(len(self.calls), 1)

    def test_runner_clears_old_completion_before_second_process(self):
        job = Job(self.config["config_path"], "bit")
        job.request["bitstream_output"] = str(job.directory / "artifacts/design.bit")
        atomic_json(job.directory / "bridge.json", dict(done="true", stage="post_route"))
        with ProjectLock(self.project, job) as lock, patch("fpga_auto.runner.run_process", return_value=0):
            code, bridge = runner.run_vivado(Path(sys.executable), Path("write_bit.tcl"), self.config, job, lock, 30)
        self.assertEqual(bridge["done"], "false")

    def test_monitor_does_not_mark_skipped_bit_done(self):
        self.report_timing = "missing"
        result = self.execute("build", True)
        snapshot = monitor.Snapshot(self.job.directory).read()
        self.assertEqual(snapshot["total_phases"], 6)
        self.assertLess(snapshot["percent"], 100)
        self.assertEqual(snapshot["phases"][-1]["status"], "stopped")
        self.assertEqual(snapshot["bitstream"]["status"], "skipped")

    def test_monitor_reports_validated_artifact(self):
        result = self.execute()
        snapshot = monitor.Snapshot(self.job.directory).read()
        self.assertEqual(snapshot["percent"], 100)
        self.assertEqual(snapshot["bitstream"], result["bitstream"])

    def test_background_preserves_build_bit_option_and_event_artifact(self):
        bat = self.root / "vivado.bat"
        fixture = Path(__file__).with_name("fake_bit_vivado.py")
        bat.write_text('@echo off\nchcp 65001 >nul\n"{}" -B -X utf8 "{}" %*\n'.format(sys.executable, fixture), encoding="utf-8")
        process = subprocess.run([sys.executable, "-B", "-X", "utf8", str(fixture.parents[1] / "portable.py"),
                                  "start", "build", "--bit", "--root", str(self.root), "--vivado", str(bat),
                                  "--no-monitor", "--json"], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        submission = json.loads(process.stdout)
        directory = Path(submission["paths"]["job"])
        deadline = time.monotonic() + 20
        while runner.process_identity(submission["worker_pid"]) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertIsNone(runner.process_identity(submission["worker_pid"]))
        result = read_json(directory / "result.json")
        self.assertEqual(exit_code(result), 0, result)
        event = read_json(directory / "completion-event.json")
        self.assertEqual(event["bitstream"]["path"], result["bitstream"]["path"])
        self.assertTrue(read_json(directory / "request.json")["generate_bitstream"])

    def test_parser_accepts_bit_and_rejects_wrong_flag_without_creating_job(self):
        self.assertTrue(portable.make_parser().parse_args(["start", "build", "--bit"]).bit)
        self.assertEqual(cli.make_parser().parse_args(["bit"]).command, "bit")
        with patch("fpga_auto.background.probe_notification") as probe:
            with self.assertRaises(FPGAError):
                background.start(self.root, self.config, "synth", sys.executable, 30, bitstream=True)
            probe.assert_not_called()
        self.assertFalse((self.root / ".fpga/jobs").exists())


if __name__ == "__main__":
    unittest.main()
