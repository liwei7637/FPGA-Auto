"""Detached operation workers and durable, explicitly addressed completion notices."""
import argparse
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid

from .jobs import Job, atomic_json, job_state, locate_job, now, read_json
from .results import FPGAError, exit_code, new_result
from .runner import process_identity
from . import desktop


def codex_executable(value=None):
    value = value or os.environ.get("CODEX_CLI_PATH") or shutil.which("codex")
    if not value or not Path(value).is_file() or Path(value).suffix.lower() != ".exe":
        raise FPGAError("notification_unavailable", "Specify --codex with the installed codex.exe path")
    return str(Path(value).resolve())


def probe_notification(executable=None, thread_id=None):
    """Probe capabilities and transport without submitting a prompt or starting a daemon."""
    if executable is None:
        try:
            result = desktop.probe(thread_id)
        except desktop.DesktopError as exc:
            raise FPGAError("notification_unavailable", str(exc)) from exc
        return dict(result, execution="completed", command="notify-check",
                    message="Desktop task is reachable; a processed completion event is still required to verify wakeup.")
    executable = codex_executable(executable)
    try:
        for command in ([executable, "queue", "--help"], [executable, "app-server", "daemon", "version"]):
            response = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                      timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            if response.returncode or ("queue" in command and not all(
                    flag in response.stdout for flag in ("--thread", "--message"))):
                raise FPGAError("notification_unavailable", (response.stderr or response.stdout)[-2000:])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FPGAError("notification_unavailable", str(exc)) from exc
    return {"execution": "completed", "command": "notify-check", "codex": executable,
            "transport": "codex_queue", "probe": "passed", "wake_verified": False,
            "message": "Transport probe passed; queue acceptance is not proof of a processed Codex turn."}


def validate_timeout(timeout):
    if not math.isfinite(timeout) or timeout <= 0:
        raise FPGAError("arguments_invalid", "--timeout must be positive and finite")


def start(root, config, operation, executable, timeout, thread_id=None, codex=None, *, verification_path=None, bitstream=False):
    validate_timeout(timeout)
    if bitstream and operation != "build":
        raise FPGAError("arguments_invalid", "--bit requires build")
    notification = {"status": "disabled"}
    if thread_id:
        try:
            thread_id = str(uuid.UUID(thread_id))
        except ValueError as exc:
            raise FPGAError("arguments_invalid", "--notify-thread must be the exact Codex task UUID") from exc
        probe = probe_notification(codex, thread_id)
        notification = {key: probe[key] for key in
                        ("transport", "pipe", "caller_thread_id", "host_id", "codex") if key in probe}
        notification.update(status="pending", thread_id=thread_id, attempts=0)
    elif codex:
        raise FPGAError("arguments_invalid", "--codex requires --notify-thread")
    job = Job(config["config_path"], operation)
    job.request.update({key: str(value) if isinstance(value, Path) else value for key, value in config.items()})
    job.request.update(background=True, root=str(root), vivado_executable=str(executable), timeout_seconds=timeout)
    job.request["generate_bitstream"] = operation == "bit" or bitstream
    if verification_path:
        job.request["wakeup_verification"] = str(verification_path)
    job.save_request()
    atomic_json(job.directory / "notification.json", notification)
    worker_script = Path(__file__).resolve().parent.parent / "worker.py"
    try:
        with (job.directory / "worker.log").open("ab", buffering=0) as log:
            # BREAKAWAY is deliberate: never pretend an attached child is independent.
            process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(worker_script), str(job.directory)],
                                       cwd=str(root), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                       close_fds=True, creationflags=subprocess.DETACHED_PROCESS |
                                       subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except OSError as exc:
        result = new_result(operation, job.id)
        result.update(reason="worker_start_failed", message=str(exc))
        job.finish(result)
        notification.update(status="not_started", error=str(exc))
        atomic_json(job.directory / "notification.json", notification)
        return result
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (job.directory / "worker-ready.json").is_file():
        if process.poll() is not None:
            break
        time.sleep(0.05)
    ready = (job.directory / "worker-ready.json").is_file()
    if not ready and process.poll() is not None:
        result = new_result(operation, job.id)
        result.update(reason="worker_start_failed", message="Worker exited before startup acknowledgement; see worker.log")
        job.finish(result)
        notification.update(status="not_started", error=result["message"])
        atomic_json(job.directory / "notification.json", notification)
        return result
    # Retain/reap Popen without tying worker lifetime to the submitter.
    threading.Thread(target=process.wait, name="fpga-worker-reaper", daemon=True).start()
    return {"execution": "submitted", "command": operation, "job_id": job.id,
            "project": str(config["project_path"]), "config": str(config["config_path"]),
            "worker_pid": process.pid, "worker_ready": ready, "notification": notification,
            "agent_action": "end_turn", "resume_trigger": "completion_notification" if thread_id else "user_request",
            "paths": {"job": str(job.directory), "result": str(job.directory / "result.json")},
            "message": "Submission is not execution success. Save the job ID and end this agent turn. "
                       "Do not poll status/logs, keep a waiting model turn, or schedule periodic model wakeups. "
                       "Python owns process waiting. Do not resubmit on a missing acknowledgement."}


def cancel(config_path, job_id):
    directory = locate_job(config_path, job_id)
    state = job_state(directory)
    if state["state"] not in ("created", "running"):
        return {"execution": "completed", "job_id": job_id, "cancel_requested": False, "state": state["state"]}
    atomic_json(directory / "cancel-request.json", {"job_id": job_id, "requested_at": now()})
    return {"execution": "completed", "job_id": job_id, "cancel_requested": True,
            "message": "Cancellation requested; terminal state and process cleanup are reported by the worker."}


def write_completion(directory, result):
    saved = read_json(directory / "result.json")
    terminal = read_json(directory / "state.json")
    if saved.get("job_id") != result["job_id"] or terminal.get("state") not in ("completed", "failed", "cancelled"):
        raise FPGAError("result_not_ready", "Completion notification requires a persisted result and terminal state")
    event = {"event_id": result["job_id"] + ":terminal", "job_id": result["job_id"],
             "operation": result["command"], "execution": result["execution"],
             "acceptance": result["acceptance"], "reason": result.get("reason"),
             "result": str(directory / "result.json"), "created_at": now()}
    event["bitstream"] = result.get("bitstream", {"status": "not_requested"})
    atomic_json(directory / "completion-event.json", event)
    return event


def deliver(directory, retry_uncertain=False):
    """One bounded delivery attempt. Never equate an accepted queue with model acknowledgement."""
    import msvcrt
    directory = Path(directory)
    with (directory / "notification.lock").open("a+b") as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b"\n")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise FPGAError("notification_busy", "Another sender is handling this event") from exc
        try:
            receipt_path = directory / "notification.json"
            receipt = read_json(receipt_path)
            if receipt["status"] in ("disabled", "queued", "suppressed", "not_started"):
                return receipt
            if receipt["status"] in ("sending", "unknown") and not retry_uncertain:
                raise FPGAError("notification_uncertain", "Delivery may have succeeded. Inspect the task before --retry-uncertain.")
            event = read_json(directory / "completion-event.json")
            if event.get("reason") == "cancelled" or (directory / "cancel-request.json").exists():
                receipt.update(status="suppressed", reason="cancelled")
                atomic_json(receipt_path, receipt)
                return receipt
            request = read_json(directory / "request.json")
            message = (
                "FPGA Auto completion event {event_id}. Operation={operation}, execution={execution}, "
                "acceptance={acceptance}. Read the result file: {result}. "
                "Verify job_id, project and current user instructions. Process this event ID only once; "
                "if already handled, do not repeat actions. Continue only work already requested by the user. "
                "A stopped/cancelled workflow must not be restarted by this message. "
                "After validating the result, record receipt in this same task with: "
                "& '{launcher}' notify-ack --config '{config}' --job '{job_id}' --event '{event_id}' --json."
            ).format(**event, launcher=str(Path(__file__).resolve().parent.parent / "fpga.cmd"),
                     config=request["config"])
            artifact = event.get("bitstream", {})
            if artifact.get("status") != "not_requested":
                message += " Bitstream status=" + artifact.get("status", "unknown") + "."
                if artifact.get("path"):
                    message += " Artifact: " + artifact["path"] + "."
            verification = request.get("wakeup_verification")
            if verification:
                message += (" This job uses a simulated build fixture solely to verify desktop wakeup, not real Vivado. "
                            "Continue the user's requested wakeup verification: inspect " + verification +
                            "; record this event's receipt and verify the original turn ended before the test stage began. "
                            "Then run the package's tests/verify_desktop_wakeup.py --verify with that evidence path "
                            "using the bundled Python. Save the verified report, update the workflow's E2E status and "
                            "PORTABLE_MANIFEST hashes to match the actual evidence. "
                            "Report whether this original desktop task was actually awakened. Do not launch synthesis "
                            "or send another test notification.")
            receipt.update(status="sending", event_id=event["event_id"], attempts=receipt.get("attempts", 0) + 1,
                           attempted_at=now(), wake_verified=False)
            atomic_json(receipt_path, receipt)
            try:
                if receipt.get("transport") == "desktop_app_tools":
                    acknowledgement = desktop.send(receipt, message)
                    receipt.update(status="queued", acknowledgement=acknowledgement)
                    atomic_json(receipt_path, receipt)
                    return receipt
                response = subprocess.run([receipt["codex"], "queue", "--thread", receipt["thread_id"],
                                           "--message", message], cwd=request["root"], capture_output=True,
                                          text=True, encoding="utf-8", errors="replace", timeout=30,
                                          creationflags=subprocess.CREATE_NO_WINDOW)
                receipt.update(status="queued" if response.returncode == 0 else "failed",
                               returncode=response.returncode, output=(response.stdout + response.stderr)[-2000:])
            except subprocess.TimeoutExpired:
                # The receiver could have accepted the event before the sender timed out.
                receipt.update(status="unknown", error="Queue timed out; not retried automatically")
            except OSError as exc:
                receipt.update(status="failed", error=str(exc))
            except desktop.DesktopError as exc:
                receipt.update(status="unknown" if exc.uncertain else "failed", error=str(exc))
            atomic_json(receipt_path, receipt)
            return receipt
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def acknowledge(config_path, job_id, event_id):
    directory = locate_job(config_path, job_id)
    receipt = read_json(directory / "notification.json")
    event = read_json(directory / "completion-event.json")
    thread_id = os.environ.get("CODEX_THREAD_ID")
    if thread_id != receipt.get("thread_id") or event_id != event["event_id"]:
        raise FPGAError("notification_mismatch", "Only the addressed desktop task may acknowledge its exact event ID")
    if receipt["status"] not in ("sending", "queued", "unknown"):
        raise FPGAError("notification_not_sent", "Cannot acknowledge an event that has not been sent")
    saved = directory / "codex-received.json"
    if saved.is_file():
        existing = read_json(saved)
        if existing.get("event_id") == event_id and existing.get("thread_id") == thread_id:
            return existing
        raise FPGAError("notification_mismatch", "Existing acknowledgement does not match this event")
    record = dict(execution="completed", job_id=job_id, event_id=event_id,
                  thread_id=thread_id, received_at=now(), wake_verified=True)
    atomic_json(saved, record)
    return record


def worker(directory):
    directory = Path(directory).resolve()
    # Exclusive claim prevents an accidental second invocation from compiling twice.
    with (directory / "worker.claim").open("x", encoding="utf-8") as claim:
        claim.write(str(os.getpid()))
    job = Job.resume(directory)
    request = job.request
    atomic_json(directory / "worker-ready.json", {"pid": os.getpid(), "identity": process_identity(os.getpid())})
    from .cli import execute
    args = argparse.Namespace(command=request["command"], config=request["config"],
                              vivado=request["vivado_executable"], timeout=request["timeout_seconds"],
                              bit=request.get("generate_bitstream", False) and request["command"] == "build")
    result = execute(args, job=job)
    # Results and terminal state are committed, and the project lock is released before notification.
    try:
        write_completion(directory, result)
        deliver(directory)
    except Exception as exc:
        atomic_json(directory / "notification-error.json", {"error": str(exc), "time": now()})
    return exit_code(result)
