"""Named debug operations shared by CLI, MCP and the session worker."""
from __future__ import annotations

import json
from pathlib import Path
import re
import time

from .analysis import analyze_ila_csv, make_snapshot
from .common import DebugError, digest, integer, json_safe, now, pause, timeout_seconds
from .profile import core_profile, probe_width


class DebugService:
    def __init__(self, backend, profile, machine):
        self.backend, self.profile, self.machine = backend, profile, machine
        self.uncertain = False
        self.job = None

    def record(self, action, phase, details):
        with (self.job.directory / "actions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(json_safe({"time": now(), "action": action, "phase": phase, **details}), ensure_ascii=False) + "\n")

    def endpoint(self, value):
        if value not in self.machine["endpoints"]:
            raise DebugError("endpoint_invalid", "Unknown explicitly bound endpoint")
        return value

    def read(self, endpoint, core, port):
        self.endpoint(endpoint)
        width = probe_width(self.profile, core, port)
        value = self.backend.read(endpoint, core, port)
        if not 0 <= value < 1 << width:
            raise DebugError("probe_invalid", "Observed probe exceeds expected width")
        return {"hex": f"0x{value:0{(width + 3) // 4}x}", "decimal": str(value), "width": width}

    def apply(self, endpoint, core, writes):
        self.endpoint(endpoint)
        core_profile(self.profile, core, "vio")
        if self.uncertain:
            raise DebugError("session_uncertain", "Previous write effect is uncertain; close and revalidate the session", uncertain=True)
        if not isinstance(writes, dict) or not writes or len(writes) > 128:
            raise DebugError("invalid_argument", "Provide a nonempty bounded map of VIO output ports")
        validated = {port: integer(value, port, 0, (1 << probe_width(self.profile, core, port, "out")) - 1)
                     for port, value in writes.items()}
        self.record("vio_apply", "intent", {"endpoint": endpoint, "core": core, "writes": {k: hex(v) for k, v in validated.items()}})
        try:
            result = self.backend.apply(endpoint, core, validated)
        except DebugError as exc:
            if exc.uncertain:
                self.uncertain = True
            self.record("vio_apply", "failed", {"reason": exc.reason, "effect_uncertain": exc.uncertain})
            raise
        self.record("vio_apply", "confirmed", {"readback": {k: hex(v) for k, v in result.items()}})
        return {k: {"hex": hex(v), "decimal": str(v)} for k, v in result.items()}

    def toggle(self, endpoint, bit):
        current = int(self.read(endpoint, "traffic", "out13")["decimal"])
        self.apply(endpoint, "traffic", {"out13": str(current ^ (1 << bit))})

    def snapshot(self, endpoint, tag="snapshot"):
        if self.profile.get("plugin") != "switch_udp_v1":
            raise DebugError("plugin_required", "UDP snapshot semantics require switch_udp_v1")
        self.toggle(endpoint, 4)
        values = []
        for page in range(8):
            self.apply(endpoint, "traffic", {"out14": page})
            raw = int(self.read(endpoint, "traffic", "in1")["decimal"])
            values.extend((raw >> (64 * i)) & ((1 << 64) - 1) for i in range(4))
        # Re-read the frozen snapshot ID; reject an external actor replacing the snapshot mid-read.
        self.apply(endpoint, "traffic", {"out14": 6})
        check_id = int(self.read(endpoint, "traffic", "in1")["decimal"]) >> 192
        if check_id != values[27]:
            raise DebugError("snapshot_changed", "Snapshot changed while reading pages")
        status = int(self.read(endpoint, "traffic", "in0")["decimal"])
        sample = make_snapshot(values, status, endpoint=endpoint, tag=tag, host_time=now())
        raw_path = self.job.directory / "snapshots.tsv"
        with raw_path.open("a", encoding="utf-8") as stream:
            for page in range(8):
                slots = values[page * 4:page * 4 + 4]
                raw = sum(v << (64 * i) for i, v in enumerate(slots))
                stream.write("\t".join([tag, endpoint, str(page)] + [str(v) for v in slots] + [f"{raw:064x}"]) + "\n")
            identity = [self.read(endpoint, "traffic", p)["hex"][2:] for p in ("in3", "in4", "in5", "in6")]
            stream.write("\t".join([tag, endpoint, "status", f"{status:08x}"] + identity) + "\n")
        return sample

    def capture(self, params):
        endpoint, core = self.endpoint(params["endpoint"]), params["core"]
        definition = core_profile(self.profile, core, "ila")
        depth = integer(params.get("depth", 1024), "depth", 1, 131072)
        if depth & (depth - 1):
            raise DebugError("invalid_argument", "ILA depth must be a power of two")
        position = integer(params.get("position", 0), "position", 0, depth - 1)
        triggers = params.get("triggers", {})
        if not isinstance(triggers, dict):
            raise DebugError("invalid_argument", "triggers must map physical input ports to compare expressions")
        for port, value in triggers.items():
            width = probe_width(self.profile, core, port, "in")
            match = re.fullmatch(r"(?:eq|neq|ne|lt|le|gt|ge)([0-9]+)'([hHbB])([0-9a-fA-FxX_]+)", str(value))
            if not match or int(match[1]) != width:
                raise DebugError("invalid_argument", "Trigger comparison width must match the full physical probe")
            digits = match[3].replace("_", "").lower()
            if (match[2].lower() == "b" and not set(digits) <= set("01x")) or \
                    int(digits.replace("x", "0"), 2 if match[2].lower() == "b" else 16) >= (1 << width):
                raise DebugError("invalid_argument", "Trigger digits exceed the probe/radix")
        immediate = params.get("immediate", not triggers)
        if not isinstance(immediate, bool) or (immediate and triggers):
            raise DebugError("invalid_argument", "Use immediate capture or explicit trigger comparisons")
        deadline = time.monotonic() + timeout_seconds(params.get("timeout", 30))
        self.record("ila_capture", "intent", params)
        complete = False
        try:
            self.backend.arm(endpoint, core, depth, position, triggers, immediate)
            while not self.backend.captured(endpoint, core):
                if time.monotonic() >= deadline:
                    raise DebugError("capture_timeout", "ILA did not complete within the requested interval")
                pause(0.05, self.job.check_cancelled)
            complete = True
            output = self.job.directory / "artifacts"
            output.mkdir()
            files = self.backend.upload(endpoint, core, output / "capture")
            analysis = analyze_ila_csv(files["csv"])
            if analysis["rows"] != depth or analysis["measurement_validity"] != "valid":
                raise DebugError("capture_invalid", "Captured rows/values do not match the requested window")
            return {"verdict": "INCONCLUSIVE", "measurement_validity": "valid", "capture_complete": True,
                    "files": {k: {"path": v, "sha256": digest(v)} for k, v in files.items() if k in ("csv", "ila")},
                    "window": {"depth": depth, "position": position, "nominal_clock_hz": definition.get("clock_hz")},
                    "analysis": analysis}
        finally:
            if not complete:
                self.backend.check = lambda: None
                try:
                    stopped = self.backend.stop_ila(endpoint, core)
                    if not stopped.get("stopped"):
                        raise DebugError("ila_stop_unconfirmed", "Backend did not confirm ILA stop")
                    self.record("ila_stop", "confirmed", {"endpoint": endpoint, "core": core, **stopped})
                    self.last_cleanup = {"ila": "stopped_partial_capture_not_accepted",
                                         "core_status": stopped.get("core_status", "not_reported")}
                except Exception as exc:
                    self.record("ila_stop", "unconfirmed", {"message": str(exc)})
                    self.last_cleanup = {"ila": "unconfirmed: " + str(exc)}

    def execute(self, operation, params, job):
        self.job = job
        self.last_cleanup = {}
        self.backend.check = job.check_cancelled
        if operation not in ("hw_status", "close"):
            begin = getattr(self.backend, "begin_operation", None)
            if begin:
                begin()
        if operation == "hw_status":
            result = {ep: self.backend.status(ep) for ep in self.machine["endpoints"]}
        elif operation == "vio_read":
            result = self.read(params["endpoint"], params["core"], params["port"])
        elif operation == "vio_apply":
            result = self.apply(params["endpoint"], params["core"], params["writes"])
        elif operation == "snapshot":
            result = self.snapshot(params["endpoint"])
        elif operation == "ila_capture":
            result = self.capture(params)
        elif operation == "recipe":
            from .plugins.switch_udp import run_recipe
            result = run_recipe(self, params)
        elif operation == "close":
            self.backend.close()
            result = {"closed": True, "traffic_stop": "not_implied_by_session_close"}
        else:
            raise DebugError("operation_invalid", "Unsupported named operation")
        return {"source_kind": self.backend.source_kind, **result}
