"""Read-only, per-job loopback dashboard; independent of the build process."""
import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import webbrowser

from .jobs import atomic_json, job_state, read_json
from .runner import process_identity

ACTIVE = ("created", "running")
PHASES = {
    "check": [("check", "工程检查")],
    "synth": [("check", "工程检查"), ("synthesis", "综合")],
    "quick": [("check", "工程检查"), ("synthesis", "新 RTL 综合"),
              ("quick_physical_check", "综合后预审查"), ("quick_placement", "快速布局，不布线"),
              ("quick_reports", "布局后风险报告")],
    "impl": [("check", "工程检查"), ("implementation", "实现 / 布线")],
    "timing": [("check", "工程检查"), ("timing", "时序验收")],
    "drc": [("check", "工程检查"), ("drc", "DRC 验收")],
    "bit": [("check", "工程检查"), ("timing", "时序验收"), ("drc", "DRC 验收"), ("bitstream", "生成 bit")],
    "build": [("check", "工程检查"), ("synthesis", "综合"),
              ("implementation", "实现 / 布线"), ("timing", "时序验收"), ("drc", "DRC 验收")],
}
LABELS = dict(PHASES["build"], preflight="准备环境", post_synth="综合报告",
              implementation_preflight="实现准备", post_route="整理报告", bitstream="生成 bit",
              quick_physical_check="综合后预审查", quick_placement="快速布局，不布线",
              quick_reports="布局后风险报告", post_place_screening="布线前预审查完成")
TAIL_BYTES = 48 * 1024


def optional_json(path):
    try:
        return read_json(path)
    except (OSError, ValueError):
        return {}


def timestamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


class Snapshot:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.bridge = {}
        self.observed_end = None

    def read(self):
        directory = self.directory
        state = job_state(directory)
        request = optional_json(directory / "request.json")
        bridge = optional_json(directory / "bridge.json")
        if bridge:
            self.bridge = bridge
        bridge = self.bridge
        result = optional_json(directory / "result.json")
        active = state["state"] in ACTIVE
        stage = bridge.get("stage", state.get("stage", "preflight"))
        operation = state.get("command", "build")
        definitions = list(PHASES.get(operation, PHASES["build"]))
        if operation == "build" and request.get("generate_bitstream"):
            definitions.append(("bitstream", "生成 bit"))
        flags = {"check": bridge.get("checked") == "true",
                 "synthesis": bridge.get("synthesis_status") == "passed",
                 "implementation": bridge.get("implementation_status") == "passed",
                 "timing": bridge.get("timing_complete") == "true",
                 "drc": bridge.get("drc_complete") == "true",
                 "bitstream": result.get("bitstream", {}).get("status") == "passed"}
        if operation == "quick":
            complete = bridge.get("quick_complete") == "true"
            flags.update(quick_physical_check=complete or bridge.get("quick_physical_complete") == "true",
                         quick_placement=complete or bridge.get("quick_placement_complete") == "true",
                         quick_reports=complete)
        # Legacy records can infer earlier phases from a later stage, but never
        # infer completion of an active run from elapsed time or log volume.
        phase_keys = [key for key, _ in definitions]
        canonical = {"post_synth": "synthesis", "implementation_preflight": "implementation"}.get(stage, stage)
        if canonical in phase_keys:
            for key in phase_keys[:phase_keys.index(canonical)]:
                flags[key] = True
        completed = state["state"] == "completed"
        phases = []
        for key, label in definitions:
            done = (completed and key != "bitstream") or flags.get(key, False)
            phases.append(dict(key=key, label=label, status="done" if done else "pending"))
        pending = next((p for p in phases if p["status"] == "pending"), None)
        if pending:
            pending["status"] = "active" if active else "stopped"
        count = sum(p["status"] == "done" for p in phases)
        # The final result is authoritative, including acceptance failures.
        percent = round(100 * count / len(phases))
        if not completed:
            percent = min(percent, 99)
        started = timestamp(state.get("start_time"))
        ended = timestamp(state.get("end_time"))
        if not active and self.observed_end is None:
            self.observed_end = time.time()
        if not active and ended is None:
            ended = self.observed_end
        elapsed = max(0, int((ended or time.time()) - started)) if started else 0
        logs = self.logs(state, bridge, started, active)
        latest = max((item["modified"] for item in logs), default=started or time.time())
        notification = state.get("notification", {})
        return dict(job_id=directory.name, project=request.get("project_path", result.get("project", "准备中")),
                    operation=operation, state=state["state"], active=active,
                    stage=stage, stage_label=LABELS.get(stage, stage), phases=phases,
                    completed_phases=count, total_phases=len(phases), percent=percent,
                    elapsed_seconds=elapsed, quiet_seconds=max(0, int(time.time() - latest)),
                    acceptance=result.get("acceptance", "pending"),
                    acceptance_scope=result.get("acceptance_scope"),
                    reason=result.get("reason") or state.get("reason"), message=result.get("message"),
                    cancel_requested=state.get("cancel_requested", False),
                    notification=notification.get("status", "disabled"),
                    logs=logs, job_directory=str(directory), demo=request.get("monitor_demo", False),
                    bitstream=result.get("bitstream", {"status": "not_started" if request.get("generate_bitstream") else "not_requested"}))

    def logs(self, state, bridge, started, active):
        directory = self.directory
        sources = [("version", "环境检查", directory / "version.log"),
                   ("main", "Vivado 主日志", directory / "vivado.log")]
        for key, label in (("synthesis", "综合子进程"), ("implementation", "实现子进程"), ("quick", "预编译布局子进程")):
            archived = directory / "reports" / (key + "_run.log")
            if archived.is_file():
                sources.append((key, label, archived))
            elif active and bridge.get(key + "_status") in ("running", "passed"):
                run_dir = bridge.get(key + "_directory")
                if run_dir:
                    live = Path(run_dir) / "runme.log"
                    try:
                        # Never show leftover output from a previous build.
                        if started and live.stat().st_mtime >= started:
                            sources.append((key, label, live))
                    except OSError:
                        pass
        sources.extend([("worker", "后台进程", directory / "worker.log"),
                        ("error", "异常详情", directory / "python_error.log")])
        logs = []
        for key, label, path in sources:
            try:
                with path.open("rb") as stream:
                    stat = os.fstat(stream.fileno())
                    offset = max(0, stat.st_size - TAIL_BYTES)
                    stream.seek(offset)
                    data = stream.read(TAIL_BYTES)
                if offset and b"\n" in data:
                    data = data.split(b"\n", 1)[1]
                content = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
                if content:
                    logs.append(dict(key=key, label=label, text=content, truncated=bool(offset),
                                     modified=stat.st_mtime, path=str(path)))
            except OSError:
                continue
        # Keep the most recently updated source at the bottom for auto-scroll.
        return sorted(logs, key=lambda item: item["modified"])


def make_server(directory, token):
    snapshot = Snapshot(directory)
    page = (Path(__file__).with_name("monitor.html")).read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            # Token lives in the path so the page and API share a strict origin.
            path = urlsplit(self.path).path
            expected_host = "127.0.0.1:" + str(self.server.server_port)
            if self.headers.get("Host") != expected_host:
                self.send_error(403)
                return
            if path not in ("/" + token + "/", "/" + token + "/snapshot"):
                self.send_error(404)
                return
            if path.endswith("/snapshot"):
                try:
                    content = json.dumps(snapshot.read(), ensure_ascii=False).encode("utf-8")
                except (OSError, ValueError, KeyError) as exc:
                    content = json.dumps({"error": "任务记录暂时无法读取：" + str(exc)}, ensure_ascii=False).encode("utf-8")
                mime = "application/json; charset=utf-8"
            else:
                content, mime = page, "text/html; charset=utf-8"
            self.server.last_visit = time.monotonic()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    server.last_visit = time.monotonic()
    return server


def serve(directory, idle_timeout=120):
    directory = Path(directory).resolve()
    token = secrets.token_urlsafe(24)
    with make_server(directory, token) as server:
        receipt = dict(status="ready", job_id=directory.name, pid=os.getpid(),
                       identity=process_identity(os.getpid()),
                       url="http://127.0.0.1:{}/{}/".format(server.server_port, token))
        atomic_json(directory / "monitor.json", receipt)
        # No tab for two minutes means the observer can exit, even if the build
        # continues. Nothing here owns or terminates the worker's process tree.
        while time.monotonic() - server.last_visit < idle_timeout:
            server.handle_request()


def launch(directory, open_browser=True):
    """UI failure is diagnostic only; it must not change a build's result."""
    directory = Path(directory).resolve()
    receipt = optional_json(directory / "monitor.json")
    try:
        if not (receipt.get("identity") and receipt.get("identity") == process_identity(receipt.get("pid"))):
            receipt = {}
            script = Path(__file__).resolve().parent.parent / "monitor.py"
            with (directory / "monitor.log").open("ab", buffering=0) as log:
                process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(script), str(directory)],
                                           cwd=str(script.parent), stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                                           creationflags=subprocess.DETACHED_PROCESS |
                                           subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB)
            threading.Thread(target=process.wait, name="fpga-monitor-reaper", daemon=True).start()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                candidate = optional_json(directory / "monitor.json")
                if candidate.get("pid") == process.pid:
                    receipt = candidate
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            if not receipt:
                raise OSError("Monitor did not become ready; see " + str(directory / "monitor.log"))
        receipt = dict(receipt)
        if open_browser:
            try:
                receipt["browser_opened"] = bool(webbrowser.open(receipt["url"]))
            except (OSError, webbrowser.Error) as exc:
                receipt.update(browser_opened=False, warning=str(exc))
        return receipt
    except (OSError, ValueError) as exc:
        return dict(status="unavailable", warning=str(exc), job_id=directory.name)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Internal read-only job monitor server")
    parser.add_argument("directory")
    parser.add_argument("--idle-timeout", type=float, default=120)
    args = parser.parse_args(argv)
    serve(args.directory, args.idle_timeout)
