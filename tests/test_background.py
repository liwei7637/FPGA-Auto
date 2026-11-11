import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fpga_auto import background
from fpga_auto.cli import execute
from fpga_auto.jobs import Job, ProjectLock, atomic_json, read_json, job_state
from fpga_auto.portable import ensure_config
from fpga_auto.results import FPGAError, new_result
from fpga_auto.runner import process_identity
from test_discovery import write_xpr


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fpga worker 中文 ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = write_xpr(self.root, "design.xpr")
        self.config, _ = ensure_config(self.root, self.root / "fpga.json", str(self.project))
        self.codex = Path(sys.executable)
        self.thread_id = "01a0a8ef-f968-7f90-a285-aa0a0400afe4"

    def notification_job(self):
        job = Job(self.config["config_path"], "synth")
        job.request.update(root=str(self.root))
        job.save_request()
        result = new_result("synth", job.id)
        result.update(execution="completed", stage="synthesis")
        job.finish(result)
        background.write_completion(job.directory, result)
        atomic_json(job.directory / "notification.json", {"status": "pending", "codex": str(self.codex),
                                                         "thread_id": self.thread_id, "attempts": 0})
        return job

    def fake_vivado(self, delay=0.2, child=False):
        atomic_json(self.root / "control.json", {"delay": delay, "child": child})
        bat = self.root / "vivado.bat"
        fixture = Path(__file__).parent / "fake_vivado.py"
        # The timeout test targets stage children, not Python/chcp startup during -version.
        bat.write_text('@echo off\nif "%~1"=="-version" (\necho Vivado v2020.2 TEST FIXTURE\nexit /b 0\n)\n'
                       'chcp 65001 >nul\n"{}" -B -X utf8 "{}" %*\n'.format(sys.executable, fixture.resolve()), encoding="utf-8")
        return bat

    def wait_file(self, path, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.is_file():
                return
            time.sleep(0.05)
        self.fail("Timed out waiting for " + str(path))

    def wait_owner_exit(self, directory):
        state = read_json(directory / "state.json")
        deadline = time.monotonic() + 8
        while process_identity(state["owner_pid"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(process_identity(state["owner_pid"]))

    def test_sender_uses_exact_thread_and_deduplicates(self):
        job = self.notification_job()
        with patch("fpga_auto.background.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "queued", "")) as run:
            receipt = background.deliver(job.directory)
            background.deliver(job.directory)
        self.assertEqual(receipt["status"], "queued")
        self.assertFalse(receipt["wake_verified"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][2:4], ["--thread", self.thread_id])
        self.assertIn(job.id + ":terminal", run.call_args.args[0][-1])

    def test_delivery_failure_is_separate_from_build_result(self):
        job = self.notification_job()
        with patch("fpga_auto.background.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "unavailable")):
            receipt = background.deliver(job.directory)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(read_json(job.directory / "result.json")["execution"], "completed")
        self.assertEqual(job_state(job.directory)["notification"]["status"], "failed")

    def test_failed_delivery_can_be_retried_without_recompiling(self):
        job = self.notification_job()
        replies = [subprocess.CompletedProcess([], 1, "", "offline"),
                   subprocess.CompletedProcess([], 0, "queued", "")]
        with patch("fpga_auto.background.subprocess.run", side_effect=replies) as run:
            background.deliver(job.directory)
            receipt = background.deliver(job.directory)
        self.assertEqual(receipt["status"], "queued")
        self.assertEqual(receipt["attempts"], 2)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(len(list((self.root / ".fpga/jobs").iterdir())), 1)

    def test_probe_only_uses_read_only_cli_commands(self):
        replies = [subprocess.CompletedProcess([], 0, "--thread --message", ""),
                   subprocess.CompletedProcess([], 0, "{}", "")]
        with patch("fpga_auto.background.subprocess.run", side_effect=replies) as run:
            report = background.probe_notification(str(self.codex))
        self.assertEqual(report["probe"], "passed")
        self.assertFalse(report["wake_verified"])
        self.assertEqual(run.call_args_list[0].args[0][1:], ["queue", "--help"])
        self.assertEqual(run.call_args_list[1].args[0][1:], ["app-server", "daemon", "version"])

    def test_cannot_publish_event_before_terminal_state(self):
        job = Job(self.config["config_path"], "synth")
        result = new_result("synth", job.id)
        atomic_json(job.directory / "result.json", result)
        with self.assertRaises(FPGAError) as raised:
            background.write_completion(job.directory, result)
        self.assertEqual(raised.exception.reason, "result_not_ready")
        self.assertFalse((job.directory / "completion-event.json").exists())

    def test_timeout_is_uncertain_and_not_automatically_retried(self):
        job = self.notification_job()
        with patch("fpga_auto.background.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 30)) as run:
            receipt = background.deliver(job.directory)
            self.assertEqual(receipt["status"], "unknown")
            with self.assertRaises(FPGAError) as raised:
                background.deliver(job.directory)
            self.assertEqual(raised.exception.reason, "notification_uncertain")
            self.assertEqual(run.call_count, 1)

    def test_cancelled_work_does_not_wake_or_restart(self):
        job = self.notification_job()
        atomic_json(job.directory / "cancel-request.json", {"job_id": job.id})
        with patch("fpga_auto.background.subprocess.run") as run:
            self.assertEqual(background.deliver(job.directory)["status"], "suppressed")
            run.assert_not_called()

    def test_probe_failure_prevents_starting_worker(self):
        with patch("fpga_auto.background.probe_notification", side_effect=FPGAError("notification_unavailable", "offline")), \
                patch("fpga_auto.background.subprocess.Popen") as popen:
            with self.assertRaises(FPGAError):
                background.start(self.root, self.config, "synth", self.codex, 30, self.thread_id)
            popen.assert_not_called()
        self.assertFalse((self.root / ".fpga/jobs").exists())

    def test_start_rejects_nonfinite_timeout(self):
        with self.assertRaises(FPGAError):
            background.start(self.root, self.config, "synth", self.codex, float("inf"))

    def test_changed_config_rejected_before_launch(self):
        job = Job(self.config["config_path"], "synth")
        job.request.update({key: str(value) if isinstance(value, Path) else value for key, value in self.config.items()})
        job.request.update(background=True, jobs=99)
        args = argparse.Namespace(command="synth", config=str(self.config["config_path"]), timeout=30, vivado=str(self.codex))
        with patch("fpga_auto.cli.run_vivado") as run:
            result = execute(args, job)
            run.assert_not_called()
        self.assertEqual(result["reason"], "config_changed")

    def test_background_outlives_submitter_and_finishes(self):
        vivado = self.fake_vivado(delay=1.5)
        portable = Path(__file__).resolve().parents[1] / "portable.py"
        response = subprocess.run([sys.executable, "-B", str(portable), "start", "synth", "--root", str(self.root),
                                   "--vivado", str(vivado), "--no-monitor", "--json"], capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(response.returncode, 0, response.stderr + response.stdout)
        accepted = json.loads(response.stdout)
        self.assertEqual(accepted["execution"], "submitted")
        directory = Path(accepted["paths"]["job"])
        self.wait_file(directory / "completion-event.json")
        self.wait_owner_exit(directory)
        self.assertEqual(read_json(directory / "result.json")["execution"], "completed")
        self.assertEqual(read_json(directory / "notification.json")["status"], "disabled")

    def test_cancel_cleans_worker_process_tree(self):
        vivado = self.fake_vivado(delay=30, child=True)
        accepted = background.start(self.root, self.config, "synth", vivado, 45)
        self.assertEqual(accepted["execution"], "submitted", accepted)
        directory = Path(accepted["paths"]["job"])
        self.wait_file(directory / "child-pid.txt")
        child_pid = int((directory / "child-pid.txt").read_text())
        background.cancel(self.config["config_path"], accepted["job_id"])
        self.wait_file(directory / "completion-event.json")
        self.wait_owner_exit(directory)
        self.assertEqual(read_json(directory / "result.json")["reason"], "cancelled")
        self.assertEqual(job_state(directory)["state"], "cancelled")
        self.assertIsNone(process_identity(child_pid))

    def test_background_timeout_preserves_failure_and_kills_children(self):
        vivado = self.fake_vivado(delay=30, child=True)
        # Budget for detached Windows startup, then time out the 30s stage itself.
        accepted = background.start(self.root, self.config, "synth", vivado, 12)
        self.assertEqual(accepted["execution"], "submitted", accepted)
        directory = Path(accepted["paths"]["job"])
        self.wait_file(directory / "completion-event.json", timeout=25)
        self.wait_owner_exit(directory)
        self.assertEqual(read_json(directory / "result.json")["reason"], "timeout")
        self.assertIsNone(process_identity(int((directory / "child-pid.txt").read_text())))

    def test_busy_project_does_not_launch_second_build(self):
        vivado = self.fake_vivado()
        owner = Job(self.config["config_path"], "synth")
        with ProjectLock(self.project, owner):
            accepted = background.start(self.root, self.config, "synth", vivado, 30)
            self.assertEqual(accepted["execution"], "submitted", accepted)
            directory = Path(accepted["paths"]["job"])
            self.wait_file(directory / "completion-event.json")
            self.wait_owner_exit(directory)
        self.assertEqual(read_json(directory / "result.json")["reason"], "project_busy")
        self.assertFalse((directory / "bridge.json").exists())

    def test_duplicate_worker_claim_cannot_run_again(self):
        job = self.notification_job()
        (job.directory / "worker.claim").write_text("already claimed")
        with patch("fpga_auto.cli.execute") as execute_mock:
            with self.assertRaises(FileExistsError):
                background.worker(job.directory)
            execute_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
