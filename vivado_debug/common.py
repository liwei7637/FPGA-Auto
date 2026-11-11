"""Shared validation and process-safe records. No hardware side effects."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from fpga_auto.jobs import atomic_json, now, read_json as _read_json
from fpga_auto.results import FPGAError


class DebugError(FPGAError):
    def __init__(self, reason, message, *, uncertain=False):
        super().__init__(reason, message, "debug")
        self.uncertain = uncertain


def sharing_retry(function):
    """Bounded NTFS sharing-violation retry; never replays a hardware operation."""
    for attempt in range(20):
        try:
            return function()
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.025)


def read_json(path):
    return sharing_retry(lambda: _read_json(path))


def integer(value, name="value", low=0, high=(1 << 64) - 1):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise DebugError("invalid_argument", name + " must be an integer or integer string")
    try:
        if isinstance(value, str):
            if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", value):
                raise ValueError()
            value = int(value, 16 if value.lower().startswith("0x") else 10)
    except ValueError as exc:
        raise DebugError("invalid_argument", "Invalid " + name) from exc
    if not low <= value <= high:
        raise DebugError("invalid_argument", f"{name} must be in [{low}, {high}]")
    return value


def timeout_seconds(value, maximum=3600):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value <= maximum:
        raise DebugError("invalid_argument", f"timeout must be positive, finite and <= {maximum}")
    return float(value)


def identifier(value, label="identifier"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,150}", value):
        raise DebugError("invalid_argument", "Invalid " + label)
    return value


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_safe(value):
    """All integers representing hardware values retain precision in JS clients."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return str(value) if abs(value) > (1 << 53) - 1 else value
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


class FileLock:
    """Persistent file + OS byte lock; never unlink a live lock file."""
    def __init__(self, path, reason="resource_busy"):
        self.path = Path(path)
        self.reason = reason
        self.stream = None

    def __enter__(self):
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if self.stream.seek(0, 2) == 0:
            self.stream.write(b"\n")
            self.stream.flush()
        self.stream.seek(0)
        try:
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise DebugError(self.reason, "Resource is owned by another operation: " + str(self.path)) from exc
        return self

    def __exit__(self, *_):
        import msvcrt
        if self.stream:
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            self.stream.close()
            self.stream = None


def pause(seconds, check=lambda: None):
    until = time.monotonic() + seconds
    while True:
        check()
        remaining = until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.05, remaining))


def error_record(exc):
    return {"execution": "failed", "reason": getattr(exc, "reason", "debug_error"),
            "message": str(exc), "effect_uncertain": getattr(exc, "uncertain", False)}


def wait_process_exit(pid, owner_identity, timeout=10):
    """Wait for the owned process handle, including Windows handle teardown."""
    import ctypes
    from ctypes import wintypes as w
    from fpga_auto.runner import process_identity
    live = process_identity(pid)
    if live and live not in (owner_identity, "unknown"):
        return  # PID was reused; never wait on another owner's process.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, int(pid))
    if not handle:
        return
    try:
        if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
            raise DebugError("session_close_pending", "Owned session process has not finished releasing resources")
    finally:
        kernel.CloseHandle(handle)
