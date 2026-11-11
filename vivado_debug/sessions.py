"""Detached persistent debug sessions and ordinary FPGA Auto completion records."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from fpga_auto import background, desktop
from fpga_auto.jobs import Job, job_state
from fpga_auto.runner import process_identity
from fpga_auto.results import new_result
from .common import DebugError, FileLock, atomic_json, error_record, identifier, json_safe, now, pause, read_json, timeout_seconds, wait_process_exit, sharing_retry
from .profile import PACKAGE, load_machine, load_profile

OPERATIONS = ("hw_status", "vio_read", "vio_apply", "snapshot", "ila_capture", "recipe", "close")


def notification_context(thread_id=None):
    if thread_id is None:
        return {"status": "disabled"}
    try:
        thread_id = str(uuid.UUID(thread_id))
    except (ValueError, TypeError) as exc:
        raise DebugError("notification_invalid", "Use the exact receiving chat UUID") from exc
    pipe = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH", "")
    if not pipe.startswith("\\\\.\\pipe\\"):
        raise DebugError("notification_unavailable", "Current desktop did not provide its notification pipe")
    # A shared MCP process must bind the receiver per request, never use its startup chat ID.
    result = desktop.request({"mode": "probe", "pipe": pipe, "caller_thread_id": thread_id, "thread_id": thread_id})
    return {"status": "pending", "transport": "desktop_app_tools", "pipe": pipe,
            "caller_thread_id": thread_id, "thread_id": thread_id, "host_id": result["hostId"], "attempts": 0}


class SessionClient:
    def __init__(self, root, *, enable_hardware=False):
        self.root = Path(root).resolve()
        self.config = self.root / "vivado-debug.json"
        self.sessions = self.root / ".fpga/debug/sessions"
        self.enable_hardware = enable_hardware

    def path(self, session_id):
        path = (self.sessions / identifier(session_id, "session ID")).resolve()
        if not path.is_relative_to(self.sessions.resolve()) or not path.is_dir():
            raise DebugError("session_invalid", "Session not found under this workspace")
        return path

    def get(self, session_id):
        state = read_json(self.path(session_id) / "state.json")
        live = process_identity(state.get("pid"))
        if state["state"] in ("opening", "ready", "busy") and state.get("pid") and (
                not live or live not in (state.get("owner_identity"), "unknown")):
            state.update(state="lost", cleanup="not_confirmed_after_worker_exit")
        return state

    def open(self, profile_path=None, machine_path=None, backend="mock", ltx_path=None,
             vivado=None, mock_scenario="normal", idle_seconds=900):
        if backend not in ("mock", "vivado"):
            raise DebugError("invalid_argument", "backend must be mock or vivado")
        if backend == "vivado" and not self.enable_hardware:
            raise DebugError("hardware_disabled", "Start the server/CLI with --enable-hardware only for an authorized board session")
        profile = load_profile(profile_path)
        machine = load_machine(machine_path, hardware=backend == "vivado")
        if backend == "vivado" and (not ltx_path or not Path(ltx_path).is_file()):
            raise DebugError("ltx_required", "Explicit original JSON LTX is required for hardware")
        idle_seconds = timeout_seconds(idle_seconds, 86400)
        session_id = "vdbg-" + uuid.uuid4().hex
        directory = self.sessions / session_id
        directory.mkdir(parents=True)
        atomic_json(directory / "session.json", {"session_id": session_id, "root": str(self.root),
                    "backend": backend, "enable_hardware": backend == "vivado" and self.enable_hardware,
                    "profile": profile, "machine": machine,
                    "ltx": str(Path(ltx_path).resolve()) if ltx_path else None,
                    "vivado": vivado, "mock_scenario": mock_scenario, "idle_seconds": idle_seconds})
        atomic_json(directory / "state.json", {"session_id": session_id, "state": "opening", "pid": None,
                    "source_kind": "simulated" if backend == "mock" else "hardware", "created_at": now()})
        try:
            with (directory / "worker.log").open("ab", buffering=0) as log:
                process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(PACKAGE / "debug_worker.py"), str(directory)],
                                           cwd=str(self.root), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           close_fds=True, creationflags=subprocess.DETACHED_PROCESS |
                                           subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB)
            threading.Thread(target=process.wait, daemon=True).start()
            until = time.monotonic() + (8 if backend == "mock" else 75)
            while time.monotonic() < until:
                state = read_json(directory / "state.json")
                if state["state"] != "opening":
                    return state
                if process.poll() is not None:
                    raise DebugError("worker_start_failed", "Session worker exited before ready; see worker.log")
                time.sleep(0.03)
            # Never start another worker on a missing acknowledgement.
            return {"session_id": session_id, "state": "opening", "message": "Startup acknowledgement pending; retain this session ID"}
        except OSError as exc:
            atomic_json(directory / "state.json", {"session_id": session_id, "state": "failed", **error_record(exc)})
            raise DebugError("worker_start_failed", str(exc)) from exc

    def submit(self, session_id, operation, params=None, notify_thread=None, timeout=120):
        if operation not in OPERATIONS:
            raise DebugError("operation_invalid", "Unsupported named operation")
        timeout = timeout_seconds(timeout)
        params = params or {}
        if not isinstance(params, dict):
            raise DebugError("invalid_argument", "params must be an object")
        receipt = notification_context(notify_thread)
        directory = self.path(session_id)
        with FileLock(directory / "submit.lock", "session_busy"):
            state = self.get(session_id)
            if state["source_kind"] == "hardware" and not self.enable_hardware:
                raise DebugError("hardware_disabled", "Hardware session operations require explicit --enable-hardware")
            if state["state"] not in ("ready", "busy"):
                raise DebugError("session_unavailable", "Session is " + state["state"])
            active = directory / "active.json"
            if active.is_file():
                previous = read_json(active)
                if read_json(Path(previous["directory"]) / "request.json").get("operation") == "close":
                    raise DebugError("session_unavailable", "Session is closing or closed")
                if not Path(previous["directory"], "result.json").is_file():
                    raise DebugError("session_busy", "Session is handling " + previous["job_id"])
            job = Job(self.config, "debug_" + operation)
            job.request.update(background=True, root=str(self.root), project=None, session_id=session_id,
                               operation=operation, params=params, timeout_seconds=timeout,
                               source_kind=state["source_kind"])
            job.save_request()
            atomic_json(job.directory / "notification.json", receipt)
            request = {"job_id": job.id, "directory": str(job.directory)}
            atomic_json(active, request)
            atomic_json(directory / "next-job.json", request)
        return {"execution": "submitted", "job_id": job.id, "session_id": session_id,
                "source_kind": state["source_kind"], "paths": {"job": str(job.directory), "result": str(job.directory / "result.json")},
                "notification": {"status": receipt["status"], "thread_id": receipt.get("thread_id")},
                "agent_action": "end_turn" if notify_thread else "read_result_on_user_request",
                "message": "Worker owns waiting; no model polling or command resubmission"}

    def job_get(self, job_id):
        identifier(job_id, "job ID")
        directory = self.root / ".fpga/jobs" / job_id
        if not directory.is_dir():
            raise DebugError("job_invalid", "Job not found")
        state = sharing_retry(lambda: job_state(directory))
        if (directory / "result.json").is_file():
            state["result"] = read_json(directory / "result.json")
        return state

    def cancel(self, job_id):
        identifier(job_id, "job ID")
        request = read_json(self.root / ".fpga/jobs" / job_id / "request.json")
        if request.get("source_kind") == "hardware" and not self.enable_hardware:
            raise DebugError("hardware_disabled", "Cancelling a hardware task requires explicit hardware mode")
        return background.cancel(self.config, job_id)

    def wait(self, job_id, timeout=30):
        until = time.monotonic() + timeout_seconds(timeout)
        while time.monotonic() < until:
            value = self.job_get(job_id)
            completion = self.root / ".fpga/jobs" / job_id / "completion-event.json"
            if value.get("result") is not None and value["state"] in ("completed", "failed", "cancelled") and completion.is_file():
                if value["result"]["command"] == "debug_close":
                    session = self.get(value["result"]["session_id"])
                    if session.get("pid"):
                        wait_process_exit(session["pid"], session.get("owner_identity"))
                return value["result"]
            time.sleep(0.03)
        raise DebugError("result_pending", "Result pending; retain job_id=" + job_id)


def worker(directory):
    from .mock import MockBackend
    from .backend import VivadoBackend
    from .service import DebugService
    directory = Path(directory).resolve()
    config = read_json(directory / "session.json")
    backend = None
    state = read_json(directory / "state.json")
    state.update(pid=os.getpid(), owner_identity=process_identity(os.getpid()))
    atomic_json(directory / "state.json", state)
    with FileLock(directory / "worker.lock", "session_busy"):
        try:
            if config["backend"] == "mock":
                backend = MockBackend(directory, config["profile"], config["machine"], config["mock_scenario"])
            else:
                backend = VivadoBackend(directory, config["profile"], config["machine"], config["ltx"],
                                        config["vivado"], enable_hardware=config["enable_hardware"])
            service = DebugService(backend, config["profile"], config["machine"])
            state.update(state="ready", profile_id=config["profile"].get("id"), ready_at=now())
            atomic_json(directory / "state.json", state)
            last_active = time.monotonic()
            while time.monotonic() - last_active < config["idle_seconds"]:
                pending = directory / "next-job.json"
                if not pending.is_file():
                    time.sleep(0.03)
                    continue
                request = read_json(pending)
                job_path = Path(request["directory"]).resolve()
                if not job_path.is_relative_to(Path(config["root"]).resolve() / ".fpga/jobs"):
                    raise DebugError("job_invalid", "Job path escaped workspace")
                with (job_path / "worker.claim").open("x", encoding="utf-8") as claim:
                    claim.write(str(os.getpid()))
                job = Job.resume(job_path)
                pending.unlink()
                state.update(state="busy", job_id=job.id)
                atomic_json(directory / "state.json", state)
                job.update(state="running", stage="debug", pid=None)
                result = new_result(job.request["command"], job.id)
                result.update(project=None, session_id=config["session_id"], stage="debug")
                cancelled = False
                service.last_cleanup = {}
                deadline = time.monotonic() + job.request["timeout_seconds"]
                original_check = job.check_cancelled

                def check():
                    original_check()
                    if time.monotonic() > deadline:
                        raise DebugError("debug_timeout", "Debug job deadline expired")

                job.check_cancelled = check
                try:
                    check()
                    result["debug"] = service.execute(job.request["operation"], job.request["params"], job)
                    result.update(execution="completed", acceptance="unknown")
                except Exception as exc:
                    result.update(error_record(exc))
                    cancelled = result["reason"] == "cancelled"
                    result["debug"] = {"source_kind": backend.source_kind, "verdict": "INCONCLUSIVE",
                                       "measurement_validity": "invalid", "cleanup": getattr(service, "last_cleanup", {})}
                result["debug"]["hardware_validated_by_this_release"] = False
                job.finish(json_safe(result), cancelled=cancelled)
                background.write_completion(job.directory, result)
                try:
                    background.deliver(job.directory)
                except Exception as exc:
                    atomic_json(job.directory / "notification-error.json", error_record(exc))
                last_active = time.monotonic()
                if job.request["operation"] == "close":
                    break
                state.update(state="ready", job_id=None, effect_uncertain=service.uncertain)
                atomic_json(directory / "state.json", state)
            state.update(state="closed", closed_at=now())
        except Exception as exc:
            state.update(state="failed", **error_record(exc))
        finally:
            if backend:
                try:
                    backend.close()
                except Exception as exc:
                    state["cleanup_error"] = str(exc)
            atomic_json(directory / "state.json", state)
    return 0 if state["state"] == "closed" else 1
