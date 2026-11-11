"""Versioned project semantics, separate from physical machine bindings."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from .common import DebugError, digest, integer, read_json

PACKAGE = Path(__file__).resolve().parent.parent


def load_profile(path=None):
    path = Path(path or PACKAGE / "profiles/switch-udp-v1.json").resolve()
    data = read_json(path)
    if data.get("schema_version") != 1 or data.get("vivado_version") != "2020.2":
        raise DebugError("profile_invalid", "Profile requires schema 1 and Vivado 2020.2")
    if not isinstance(data.get("cores"), dict) or not data["cores"]:
        raise DebugError("profile_invalid", "Profile requires named debug cores")
    names = set()
    for alias, core in data["cores"].items():
        if core.get("kind") not in ("ila", "vio") or not isinstance(core.get("cell"), str) or not core["cell"]:
            raise DebugError("profile_invalid", "Invalid core " + alias)
        if core["cell"] in names:
            raise DebugError("profile_invalid", "Duplicate core cell")
        names.add(core["cell"])
        uuid = core.get("uuid")
        if not isinstance(uuid, str) or not re.fullmatch(r"[A-Fa-f0-9]{32}", uuid):
            raise DebugError("profile_invalid", "Invalid core UUID")
        for port, width in core.get("ports", {}).items():
            if not re.fullmatch(r"(?:in|out)[0-9]+", port):
                raise DebugError("profile_invalid", "Invalid physical probe " + port)
            core["ports"][port] = integer(width, "probe width", 1, 4096)
    data["_path"] = str(path)
    data["_sha256"] = digest(path)
    return data


def mock_machine():
    return {"schema_version": 1, "server": "mock://local", "endpoints": {
        "A": {"target": "SIMULATED-A", "dna": "000000000000000000000001", "ip": "192.168.0.129", "mac": "02:00:00:00:00:81"},
        "B": {"target": "SIMULATED-B", "dna": "000000000000000000000002", "ip": "192.168.0.128", "mac": "02:00:00:00:00:80"}}}


def load_machine(path=None, *, hardware=False):
    if path is None:
        if hardware:
            raise DebugError("machine_required", "Hardware requires explicit target + DNA bindings")
        return mock_machine()
    data = read_json(path)
    if data.get("schema_version") != 1 or not isinstance(data.get("endpoints"), dict) or not data["endpoints"]:
        raise DebugError("machine_invalid", "Machine requires schema 1 and endpoints")
    server = data.get("server", "")
    if not isinstance(server, str) or (hardware and not re.fullmatch(r"[A-Za-z0-9_.-]+:[0-9]{1,5}", server)):
        raise DebugError("machine_invalid", "Expected an explicit hw_server host:port")
    targets, dnas = set(), set()
    for alias, endpoint in data["endpoints"].items():
        target, dna = endpoint.get("target", ""), endpoint.get("dna", "")
        if not isinstance(target, str) or not target or any(c in target for c in "*?[]\r\n\x00"):
            raise DebugError("machine_invalid", "Use exact target paths, without wildcard selectors")
        if not isinstance(dna, str) or not re.fullmatch(r"[A-Fa-f0-9]{24}", dna):
            raise DebugError("machine_invalid", "DNA must contain exactly 24 hex digits")
        if target in targets or dna.upper() in dnas:
            raise DebugError("machine_invalid", "Endpoint target and DNA must be unique")
        targets.add(target)
        dnas.add(dna.upper())
        endpoint["dna"] = dna.upper()
    return data


def core_profile(profile, alias, kind=None):
    core = profile["cores"].get(alias)
    if core is None or (kind and core["kind"] != kind):
        raise DebugError("core_invalid", "Unknown or wrong-kind core: " + str(alias))
    return core


def probe_width(profile, core, port, direction=None):
    c = core_profile(profile, core)
    if direction and not str(port).startswith(direction):
        raise DebugError("probe_direction", "Expected a " + direction + " probe")
    if port not in c.get("ports", {}):
        raise DebugError("probe_invalid", "Probe not in profile: " + str(port))
    return c["ports"][port]
