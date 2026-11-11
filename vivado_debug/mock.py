"""Deterministic hardware model for offline protocol/recipe tests, never board evidence."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import time

from .common import DebugError

SCENARIOS = ("normal", "drop_tail", "not_ready", "device_lost", "write_timeout", "ila_timeout", "stop_stuck")


class MockBackend:
    source_kind = "simulated"

    def __init__(self, directory, profile, machine, scenario="normal"):
        if scenario not in SCENARIOS:
            raise DebugError("invalid_argument", "Unknown mock scenario")
        self.profile, self.machine, self.scenario = profile, machine, scenario
        self.check = lambda: None
        self.states = {ep: {"out": {"out" + str(i): 0 for i in range(15)}, "values": [0] * 32,
                            "frozen": [0] * 32, "valid": False, "snapshot_valid": False,
                            "running": False, "done": False, "errors": 0, "fraction": 0.0,
                            "last_command": 0, "sent": 0, "dropped": False} for ep in machine["endpoints"]}
        self.last_time = time.monotonic()
        self.armed = {}
        self.generic_values = {}
        self.write_failed = False

    def advance(self):
        self.check()
        if self.scenario == "device_lost":
            raise DebugError("session_lost", "SIMULATED device disappearance")
        stamp = time.monotonic()
        dt, self.last_time = stamp - self.last_time, stamp
        for state in self.states.values():
            state["values"][16] += int(dt * 200000000)
        for ep, state in self.states.items():
            if not state["running"]:
                continue
            out = state["out"]
            payload = out["out7"]
            pps = (8.47194e9 if out["out12"] else 9.571e9) / (8 * payload)
            amount = dt * pps + state["fraction"]
            count, state["fraction"] = int(amount), amount % 1
            limit = out["out11"]
            if limit:
                count = min(count, limit - state["sent"])
            state["sent"] += count
            state["values"][0] += count
            state["values"][1] += count * payload
            for peer_ep, peer in self.states.items():
                if peer_ep == ep or not peer["valid"] or not peer["out"]["out13"] & 0x20:
                    continue
                accepted = count
                if self.scenario == "drop_tail" and count and not state["dropped"]:
                    accepted -= 1
                    state["dropped"] = True
                peer["values"][2] += accepted
                peer["values"][3] += accepted
                peer["values"][5] += accepted * payload
                peer["values"][28] += accepted * payload
            if limit and state["sent"] >= limit:
                state["running"], state["done"] = False, True

    def status_word(self, endpoint):
        s = self.states[endpoint]
        command = s["out"]["out13"]
        ready = bool(command & 0x40) and self.scenario != "not_ready"
        return ((command >> 5) & 3) | (4 if ready else 0) | (8 if s["valid"] else 0) | \
            (0x30 if s["running"] else 0) | (0x40 if s["done"] else 0) | 0x80 | \
            (0x100 if s["snapshot_valid"] else 0) | s["errors"]

    def status(self, endpoint):
        self.advance()
        return {"endpoint": endpoint, "dna": self.machine["endpoints"][endpoint]["dna"], "done": "1",
                "crc_error": "0", "source_kind": "simulated"}

    def read(self, endpoint, core, port):
        self.advance()
        if self.profile["cores"][core]["kind"] != "vio":
            raise DebugError("core_invalid", "Expected VIO")
        if self.profile.get("plugin") != "switch_udp_v1":
            return self.generic_values.get((endpoint, core, port), 0)
        state = self.states[endpoint]
        if port.startswith("out"):
            return state["out"].get(port, 0)
        if port == "in0":
            return self.status_word(endpoint)
        if port == "in1":
            page = state["out"]["out14"]
            return sum(v << (64 * slot) for slot, v in enumerate(state["frozen"][page * 4:page * 4 + 4]))
        return state["out"].get({"in3": "out0", "in4": "out1", "in5": "out2", "in6": "out3"}.get(port, ""), 0)

    def apply(self, endpoint, core, writes):
        self.advance()
        if self.profile.get("plugin") != "switch_udp_v1":
            self.generic_values.update({(endpoint, core, port): value for port, value in writes.items()})
            return dict(writes)
        state = self.states[endpoint]
        state["out"].update(writes)
        if "out13" in writes:
            command = writes["out13"]
            changed = command ^ state["last_command"]
            state["last_command"] = command
            if changed & 1:
                if state["running"]:
                    state["errors"] |= 0x1000
                elif not 32 <= state["out"]["out7"] <= 1472:
                    state["errors"] |= 0x2000
                else:
                    state["valid"] = True
            if changed & 8:
                if state["running"]:
                    state["errors"] |= 0x8000
                else:
                    state.update(values=[0] * 32, frozen=[0] * 32, snapshot_valid=False, errors=0, sent=0, dropped=False)
            if changed & 2:
                if not state["valid"] or not self.status_word(endpoint) & 4 or state["running"]:
                    state["errors"] |= 0x4000
                else:
                    state.update(running=True, done=False, sent=0)
            if changed & 4 and self.scenario != "stop_stuck":
                state.update(running=False, done=True)
            if changed & 16:
                state["values"][27] += 1
                state["frozen"] = state["values"][:]
                state["snapshot_valid"] = True
        if self.scenario == "write_timeout" and not self.write_failed:
            self.write_failed = True
            raise DebugError("debug_timeout", "SIMULATED commit happened but reply was lost", uncertain=True)
        return {port: state["out"][port] for port in writes}

    def arm(self, endpoint, core, depth, position, triggers, immediate):
        self.check()
        self.armed[(endpoint, core)] = (depth, position)
        return {"armed": True}

    def captured(self, endpoint, core):
        self.check()
        return self.scenario != "ila_timeout"

    def stop_ila(self, endpoint, core):
        self.armed.pop((endpoint, core), None)
        return {"stopped": True}

    def upload(self, endpoint, core, output):
        self.check()
        depth, _ = self.armed[(endpoint, core)]
        output = Path(output)
        csv_path, ila_path = output.with_suffix(".csv"), output.with_suffix(".ila")
        with csv_path.open("x", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["Sample in Buffer"] + list(self.profile["cores"][core]["ports"]))
            writer.writerow(["Radix"] + ["HEX"] * len(self.profile["cores"][core]["ports"]))
            for i in range(depth):
                writer.writerow([i] + [f"{i & ((1 << w) - 1):x}" for w in self.profile["cores"][core]["ports"].values()])
        ila_path.write_text(json.dumps({"SIMULATED_CAPTURE": True, "not_a_vivado_binary": True, "depth": depth}), encoding="utf-8")
        return {"csv": str(csv_path), "ila": str(ila_path), "source_kind": "simulated"}

    def close(self):
        pass
