"""Foreground process execution with a Windows kill-on-close process tree."""
import ctypes
from ctypes import wintypes as w
import json
import os
import subprocess
import shutil
import time
from pathlib import Path
from .results import FPGAError


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    return kernel


def process_identity(pid):
    if not pid:
        return None
    kernel = _kernel()
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return "unknown" if ctypes.get_last_error() == 5 else None
    try:
        values = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(x) for x in values]):
            return "unknown"
        if values[1].dwLowDateTime or values[1].dwHighDateTime:
            return None
        return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", w.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                ("active_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", w.DWORD), ("scheduling", w.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [("user_time", ctypes.c_int64), ("kernel_time", ctypes.c_int64),
                ("period_user", ctypes.c_int64), ("period_kernel", ctypes.c_int64),
                ("page_faults", w.DWORD), ("total_processes", w.DWORD),
                ("active_processes", w.DWORD), ("terminated_processes", w.DWORD)]


class ProcessTree:
    def __init__(self):
        self.kernel = _kernel()
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                         w.DWORD, ctypes.c_void_p]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def attach_and_resume(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.kill()
            process.wait()
            raise ctypes.WinError(ctypes.get_last_error())
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [w.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long
        if ntdll.NtResumeProcess(int(process._handle)) != 0:
            raise OSError("Could not resume owned process")

    def close(self):
        if self.handle:
            try:
                if not self.kernel.TerminateJobObject(self.handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                deadline = time.monotonic() + 15
                while True:
                    accounting = _Accounting()
                    if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting),
                                                                 ctypes.sizeof(accounting), None):
                        raise ctypes.WinError(ctypes.get_last_error())
                    if accounting.active_processes == 0:
                        break
                    if time.monotonic() > deadline:
                        raise OSError("Process tree termination did not complete")
                    time.sleep(0.025)
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


def run_process(command, log_path, cwd, timeout, on_start=None, on_tick=None, env=None):
    if os.name != "nt":
        raise FPGAError("platform_unsupported", "V1 supports Windows only")
    tree = ProcessTree()
    process = None
    started = time.monotonic()
    try:
        with open(log_path, "ab", buffering=0) as log:
            process = subprocess.Popen(command, cwd=str(cwd), env=env,
                                       stdout=log, stderr=subprocess.STDOUT,
                                       creationflags=0x4 | subprocess.CREATE_NO_WINDOW)
            tree.attach_and_resume(process)
            if on_start:
                on_start(process.pid)
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise FPGAError("timeout", "Process exceeded {} seconds".format(timeout))
                try:
                    return process.wait(timeout=min(1.0, remaining))
                except subprocess.TimeoutExpired:
                    if on_tick:
                        on_tick()
    except OSError as exc:
        raise FPGAError("vivado_start_failed", str(exc)) from exc
    finally:
        tree.close()
        if process is not None:
            process.wait(timeout=15)


def read_bridge(job):
    try:
        value = json.loads((job.directory / "bridge.json").read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def run_vivado(executable, script, config, job, lock, timeout):
    from .vivado import command_line
    from .jobs import atomic_json
    env = dict(os.environ)
    # Values are data, not interpolated Tcl or shell code.
    env.update(FPGA_PROJECT=str(config["project_path"]), FPGA_SYNTH=config["synth_run"],
               FPGA_IMPL=config["impl_run"], FPGA_JOBS=str(config["jobs"]),
               FPGA_JOB_DIR=str(job.directory), FPGA_OPERATION=script.stem,
               FPGA_BITSTREAM_REQUESTED="1" if job.request.get("generate_bitstream") else "0")
    if script.stem == "write_bit":
        env["FPGA_BITSTREAM_OUTPUT"] = job.request["bitstream_output"]
        initial = dict(read_bridge(job), stage="bitstream", done="false", bitstream_status="running")
        for key in ("reason", "message"):
            initial.pop(key, None)
    else:
        initial = dict(stage="preflight", done="false")
    # A later Vivado invocation must never consume a previous done=true record.
    atomic_json(job.directory / "bridge.json", initial)
    args = ["-mode", "batch", "-notrace", "-nojournal", "-nolog", "-source", str(script)]

    def on_start(pid):
        lock.set_process(pid)
        job.update(state="running", pid=pid)
        job.check_cancelled()

    def on_tick(check_cancel=True):
        if check_cancel:
            job.check_cancelled()
        bridge = read_bridge(job)
        stage = bridge.get("stage")
        if stage and stage != job.state["stage"]:
            job.update(stage=stage)

    try:
        code = run_process(command_line(executable, args), job.log, job.directory,
                           timeout, on_start, on_tick, env)
    finally:
        on_tick(check_cancel=False)
        bridge = read_bridge(job)
        for stage in ("synthesis", "implementation", "quick"):
            directory = bridge.get(stage + "_directory")
            if directory and bridge.get(stage + "_status"):
                source = Path(directory) / "runme.log"
                if source.is_file():
                    shutil.copy2(str(source), str(job.directory / "reports" / (stage + "_run.log")))
    job.update(returncode=code)
    return code, read_bridge(job)
