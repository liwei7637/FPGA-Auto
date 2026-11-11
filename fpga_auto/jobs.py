"""Atomic job records and OS-held project locks."""
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .results import FPGAError


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for attempt in range(20):
            try:
                os.replace(str(temporary), str(path))
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.025)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object: " + str(path))
    return value


class Job:
    def __init__(self, config_path, command):
        from .runner import process_identity
        self.root = Path(config_path).resolve().parent / ".fpga"
        self.id = datetime.now().strftime("%Y%m%d_%H%M%S_%f_") + command + "_" + uuid.uuid4().hex[:6]
        self.directory = self.root / "jobs" / self.id
        (self.directory / "reports").mkdir(parents=True)
        self.log = self.directory / "vivado.log"
        self.log.touch()
        self.state = dict(job_id=self.id, command=command, state="created", stage="preflight",
                          start_time=now(), end_time=None, pid=None, owner_pid=os.getpid(),
                          owner_identity=process_identity(os.getpid()))
        self.request = dict(self.state, config=str(Path(config_path).resolve()))
        self.save_request()
        self.update()

    def save_request(self):
        atomic_json(self.directory / "request.json", self.request)

    @classmethod
    def resume(cls, directory):
        from .runner import process_identity
        self = cls.__new__(cls)
        self.directory = Path(directory).resolve()
        self.root = self.directory.parent.parent
        self.id = self.directory.name
        self.log = self.directory / "vivado.log"
        self.request = read_json(self.directory / "request.json")
        self.state = read_json(self.directory / "state.json")
        if not self.request.get("background") or self.state["state"] != "created":
            raise FPGAError("job_invalid", "Only a newly submitted background job can be resumed")
        self.update(owner_pid=os.getpid(), owner_identity=process_identity(os.getpid()))
        return self

    def check_cancelled(self):
        if (self.directory / "cancel-request.json").is_file():
            raise FPGAError("cancelled", "Cancellation requested", self.state["stage"])

    def update(self, **fields):
        self.state.update(fields)
        atomic_json(self.directory / "state.json", self.state)

    def finish(self, result, cancelled=False):
        result["paths"].update(job=str(self.directory), log=str(self.log),
                               reports=str(self.directory / "reports"))
        result["start_time"] = self.state["start_time"]
        result["end_time"] = now()
        atomic_json(self.directory / "result.json", result)
        self.update(state="cancelled" if cancelled else result["execution"],
                    stage=result["stage"], end_time=result["end_time"], reason=result.get("reason"))


class ProjectLock:
    """All Vivado access is serialized; status/logs never acquire this lock.

    A persistent inode plus a one-byte OS lock avoids unlink/recreate races.
    Windows releases the lock on owner death. The process Job Object then kills
    descendants; a live recorded child conservatively prevents stale recovery.
    """
    def __init__(self, project, job):
        self.project = Path(project).resolve()
        self.path = Path(project).resolve().parent / ".fpga" / "project.lock"
        self.job = job
        self.stream = None

    def __enter__(self):
        import msvcrt
        from .runner import process_identity
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = open(self.path, "a+b")
        if self.stream.seek(0, 2) == 0:
            self.stream.write(b"\n")
            self.stream.flush()
        self.stream.seek(0)
        try:
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise FPGAError("project_busy", "Another task holds " + str(self.path)) from exc
        try:
            self.stream.seek(1)
            previous = self.stream.read()
            if previous:
                try:
                    previous = json.loads(previous.decode("utf-8"))
                except ValueError:
                    previous = {}
                if not isinstance(previous, dict):
                    previous = {}
                pid = previous.get("pid")
                identity = previous.get("identity")
                live = process_identity(pid)
                if previous.get("active") and live and live in (identity, "unknown"):
                    raise FPGAError("project_busy", "Previous Vivado process is still alive")
            self.metadata = dict(job_id=self.job.id, active=True, owner_pid=os.getpid(),
                                 project=str(self.project))
            self.write()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def write(self):
        self.stream.seek(1)
        # a+b forces writes to EOF; truncate first so metadata replaces old data.
        self.stream.truncate(1)
        self.stream.write(json.dumps(self.metadata).encode("utf-8"))
        self.stream.flush()

    def set_process(self, pid):
        from .runner import process_identity
        self.metadata.update(pid=pid, identity=process_identity(pid))
        self.write()

    def __exit__(self, *_):
        import msvcrt
        if self.stream:
            try:
                if hasattr(self, "metadata"):
                    from .runner import process_identity
                    live = process_identity(self.metadata.get("pid"))
                    self.metadata["active"] = bool(live and live in (self.metadata.get("identity"), "unknown"))
                    self.write()
            finally:
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                self.stream.close()
                self.stream = None


def locate_job(config_path, job_id=None):
    root = Path(config_path).resolve().parent / ".fpga" / "jobs"
    if job_id:
        if Path(job_id).name != job_id or any(c in job_id for c in "/\\:") or job_id in (".", ".."):
            raise FPGAError("job_invalid", "Use a job ID, not a path")
        directory = root / job_id
    else:
        candidates = sorted(root.glob("*/state.json"), reverse=True)
        if not candidates:
            raise FPGAError("job_missing", "No recorded jobs")
        running = []
        for candidate in candidates:
            state = job_state(candidate.parent)
            if state.get("state") in ("created", "running"):
                running.append(candidate)
        directory = (running or candidates)[0].parent
    if not (directory / "state.json").is_file():
        raise FPGAError("job_missing", "Job not found: " + str(directory))
    return directory


def job_state(directory):
    from .runner import process_identity
    state = read_json(directory / "state.json")
    if state["state"] in ("created", "running"):
        identity = process_identity(state.get("owner_pid"))
        if identity is None or identity != state.get("owner_identity") and identity != "unknown":
            final_path = directory / "result.json"
            if final_path.is_file():
                final = read_json(final_path)
                state.update(state="cancelled" if final.get("reason") == "cancelled" else final["execution"],
                             reason=final.get("reason"), end_time=final.get("end_time"), observed_state=True)
            else:
                state.update(state="failed", reason="owner_exited", observed_state=True)
    notification = directory / "notification.json"
    if notification.is_file():
        state["notification"] = read_json(notification)
        acknowledgement = directory / "codex-received.json"
        if acknowledgement.is_file():
            ack = read_json(acknowledgement)
            if (ack.get("event_id") == directory.name + ":terminal" and
                    ack.get("thread_id") == state["notification"].get("thread_id")):
                state["notification"].update(wake_verified=True, received_at=ack["received_at"])
    state["cancel_requested"] = (directory / "cancel-request.json").is_file()
    state["result"] = str(directory / "result.json") if (directory / "result.json").is_file() else None
    return state
