"""Supervised Vivado 2020.2 Tcl mailbox. Constructed only in explicit hardware mode."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

from fpga_auto.runner import ProcessTree
from fpga_auto.vivado import command_line, find_vivado
from .common import DebugError, FileLock, atomic_json, read_json
from .ltx import normalize


class VivadoBackend:
    source_kind = "hardware"

    def __init__(self, directory, profile, machine, ltx, vivado=None, *, enable_hardware=False):
        if not enable_hardware:
            raise DebugError("hardware_disabled", "Hardware backend is disabled; use mock for offline work")
        if not isinstance(profile.get("part"), str) or not profile["part"]:
            raise DebugError("profile_invalid", "Hardware profile requires an explicit short PART")
        self.directory = Path(directory)
        self.profile = profile
        self.machine = machine
        self.check = lambda: None
        self.sequence = 0
        self.mutex = threading.RLock()
        self.poisoned = False
        self.process = None
        self.tree = None
        self.log = None
        self.resources = ExitStack()
        try:
            for dna in sorted(x["dna"] for x in machine["endpoints"].values()):
                self.resources.enter_context(FileLock(Path(tempfile.gettempdir()) / "vivado-debug-locks" / (dna + ".lock"), "hardware_busy"))
            evidence = normalize(ltx, self.directory / "physical-probes.ltx", profile)
            atomic_json(self.directory / "ltx-identity.json", evidence)
            mailbox = self.directory / "bridge"
            mailbox.mkdir()
            self.mailbox = mailbox
            executable = find_vivado(vivado)
            script = Path(__file__).resolve().parent.parent / "tcl/debug_bridge.tcl"
            self.log = (self.directory / "vivado-debug.log").open("ab", buffering=0)
            self.tree = ProcessTree()
            self.process = subprocess.Popen(command_line(executable, ["-mode", "batch", "-notrace", "-nojournal", "-nolog",
                                                                     "-source", str(script), "-tclargs", str(mailbox)]),
                                            cwd=str(self.directory), stdin=subprocess.DEVNULL,
                                            stdout=self.log, stderr=subprocess.STDOUT,
                                            creationflags=0x4 | subprocess.CREATE_NO_WINDOW)
            self.tree.attach_and_resume(self.process)
            ready = self.wait_file(mailbox / "ready.json", 60)
            if ready.get("vivado_version") != "2020.2" or ready.get("missing_commands"):
                raise DebugError("vivado_incompatible", "Vivado 2020.2 debug commands not available")
            # Vivado 2020.2 may launch both hw_server and cs_server here;
            # a measured cold start takes longer than the ordinary 20 s mailbox limit.
            self.call("connect", server=machine["server"], timeout=60)
            for alias, core in profile["cores"].items():
                self.call("bind_core", alias=alias, kind=core["kind"], cell=core["cell"], uuid=core["uuid"])
            for endpoint, binding in machine["endpoints"].items():
                self.call("bind", endpoint=endpoint, target=binding["target"], dna=binding["dna"],
                          part=profile.get("part", "xcku040"), ltx=evidence["derived_path"])
            # Prove all physical widths before accepting any caller write.
            for endpoint in machine["endpoints"]:
                observed_status = self.status(endpoint)
                if observed_status["done"] != "1" or observed_status["crc_error"] != "0":
                    continue
                for alias, core in profile["cores"].items():
                    if core["kind"] == "vio":
                        for port, width in core["ports"].items():
                            if self.call("vio_read", endpoint=endpoint, core=alias, port=port)["width"] != width:
                                raise DebugError("probe_mismatch", "Live VIO width differs from profile")
        except BaseException:
            self.close()
            raise

    def wait_file(self, path, timeout):
        until = time.monotonic() + timeout
        while not path.is_file():
            self.check()
            if self.process.poll() is not None:
                raise DebugError("session_lost", "Owned Vivado exited; see session log")
            if time.monotonic() >= until:
                self.poisoned = True
                raise DebugError("debug_timeout", "Vivado reply timed out; command effect is uncertain", uncertain=True)
            time.sleep(0.02)
        return read_json(path)

    def call(self, operation, *, timeout=20, **params):
        with self.mutex:
            if self.poisoned:
                raise DebugError("session_uncertain", "Timed-out session cannot replay hardware commands", uncertain=True)
            self.sequence += 1
            wire = "\n".join(k + "\t" + str(v).encode("utf-8").hex()
                             for k, v in {"operation": operation, **params}.items()) + "\n"
            path = self.mailbox / f"request-{self.sequence:08d}.req"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(wire, encoding="utf-8")
            os.replace(temporary, path)
            try:
                result = self.wait_file(self.mailbox / f"reply-{self.sequence:08d}.json", timeout)
            except Exception as exc:
                self.poisoned = True
                raise DebugError(getattr(exc, "reason", "debug_timeout"), str(exc),
                                 uncertain=operation in ("vio_set", "vio_commit", "ila_arm")) from exc
            if not result.get("ok"):
                raise DebugError(result.get("reason", "vivado_debug_failed"), result.get("message", "Unknown Vivado error"),
                                 uncertain=operation in ("vio_commit", "ila_arm"))
            return result["result"]

    def status(self, endpoint):
        return self.call("status", endpoint=endpoint)

    def begin_operation(self):
        # Revalidate live configuration once per logical job, before touching
        # probes. Refreshing between individual VIO/ILA steps discards state.
        for endpoint in self.machine["endpoints"]:
            self.status(endpoint)

    def read(self, endpoint, core, port):
        return int(self.call("vio_read", endpoint=endpoint, core=core, port=port)["value"], 16)

    def apply(self, endpoint, core, writes):
        try:
            for port, value in writes.items():
                width = self.profile["cores"][core]["ports"][port]
                self.call("vio_set", endpoint=endpoint, core=core, port=port, value=f"{value:0{(width + 3) // 4}x}")
            self.call("vio_commit", endpoint=endpoint, core=core)
            observed = {port: self.read(endpoint, core, port) for port in writes}
            if observed != writes:
                raise DebugError("write_readback_mismatch", "VIO outputs differ after commit", uncertain=True)
            return observed
        except Exception as exc:
            # Partially staged OUTPUT_VALUE properties must never leak into a later commit.
            self.poisoned = True
            raise DebugError(getattr(exc, "reason", "write_failed"), str(exc), uncertain=True) from exc

    def arm(self, endpoint, core, depth, position, triggers, immediate):
        params = {"endpoint": endpoint, "core": core, "depth": depth, "position": position,
                  "trigger_count": len(triggers), "immediate": int(immediate)}
        for i, (port, value) in enumerate(triggers.items()):
            params.update({f"trigger_{i}_port": port, f"trigger_{i}_value": value})
        return self.call("ila_arm", **params)

    def captured(self, endpoint, core):
        return self.call("ila_wait", endpoint=endpoint, core=core, timeout=5)["complete"]

    def stop_ila(self, endpoint, core):
        return self.call("ila_stop", endpoint=endpoint, core=core)

    def upload(self, endpoint, core, output):
        return self.call("ila_upload", endpoint=endpoint, core=core, output=str(output), timeout=60)

    def close(self):
        try:
            if self.process and self.process.poll() is None and not self.poisoned:
                try:
                    self.check = lambda: None
                    self.call("quit", timeout=5)
                except Exception:
                    pass
            if self.tree:
                self.tree.close()
                self.tree = None
            if self.process:
                self.process.wait(timeout=15)
        finally:
            if self.log:
                self.log.close()
            self.resources.close()
