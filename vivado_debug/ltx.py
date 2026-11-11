"""Read Vivado JSON LTX and create display-only physical-port aliases."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from .common import DebugError, atomic_json, digest, integer, read_json


def describe(path):
    try:
        data = read_json(path)
        groups = data["ltx_root"]["ltx_data"]
        cores = []
        for group in groups:
            for core in group.get("debug_cores", []):
                if not str(core.get("type", "")).startswith(("ILA", "VIO")):
                    continue
                ports = {}
                occupied = {}
                for pin in core.get("pins", []):
                    direction = pin["direction"].lower()
                    if direction not in ("in", "out"):
                        raise ValueError("Unknown probe direction")
                    index = integer(pin["portIndex"], "port index", 0, 1023)
                    left = integer(pin.get("leftIndex", 0), "left index", 0, 4095)
                    right = integer(pin.get("rightIndex", left), "right index", 0, 4095)
                    port = direction + str(index)
                    bits = set(range(min(left, right), max(left, right) + 1))
                    if occupied.setdefault(port, set()) & bits:
                        raise ValueError("Overlapping physical probe bits")
                    occupied[port].update(bits)
                    ports[port] = max(ports.get(port, 0), max(left, right) + 1)
                for port, width in ports.items():
                    if occupied[port] != set(range(width)):
                        raise ValueError("Incomplete physical probe port: " + port)
                uuid = str(core["uuid"]).upper()
                if any(x["cell"] == core["name"] or x["uuid"] == uuid for x in cores):
                    raise ValueError("Duplicate core cell or UUID")
                cores.append({"cell": core["name"], "uuid": uuid,
                              "kind": "vio" if core["type"].startswith("VIO") else "ila", "ports": ports})
        if not cores:
            raise ValueError("No ILA/VIO cores found")
        return {"format": "vivado-json-ltx", "sha256": digest(path), "cores": cores}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DebugError("ltx_invalid", "Unsupported or malformed Vivado JSON LTX: " + str(exc)) from exc


def validate_profile(description, profile):
    by_cell = {c["cell"]: c for c in description["cores"]}
    for alias, expected in profile["cores"].items():
        found = by_cell.get(expected["cell"])
        if not found or found["kind"] != expected["kind"]:
            raise DebugError("ltx_mismatch", "Missing expected core " + alias)
        if expected.get("uuid") and found["uuid"] != expected["uuid"].upper():
            raise DebugError("ltx_mismatch", "Core UUID mismatch: " + alias)
        if found["ports"] != expected["ports"]:
            raise DebugError("ltx_mismatch", "Physical port widths differ: " + alias)


def normalize(source, destination, profile):
    description = describe(source)
    validate_profile(description, profile)
    destination = Path(destination)
    if destination.exists():
        raise DebugError("output_exists", "Preserve existing derived LTX")
    data = read_json(source)
    by_cell = {c["cell"]: c for c in description["cores"]}
    for group in data["ltx_root"]["ltx_data"]:
        for core in group.get("debug_cores", []):
            if core.get("name") not in by_cell:
                continue
            info = by_cell[core["name"]]
            pins = []
            for port, width in sorted(info["ports"].items()):
                direction = "OUT" if port.startswith("out") else "IN"
                index = int(port[3:] if direction == "OUT" else port[2:])
                name = "vdbg_" + info["uuid"] + "_" + port
                pins.append({"name": name, "id": len(pins),
                             "type": "DATA" if info["kind"] == "vio" else "DATA_TRIGGER",
                             "direction": direction, "isVector": True, "leftIndex": 0,
                             "rightIndex": width - 1, "portIndex": index,
                             "nets": [{"name": name, "isBus": True,
                                       "subnets": [{"name": f"{name}[{i}]"} for i in range(width - 1, -1, -1)]}]})
            core["pins"] = pins
    atomic_json(destination, data)
    return {"original_sha256": description["sha256"], "derived_sha256": digest(destination),
            "derived_path": str(destination.resolve()), "purpose": "display aliases only; original preserved",
            "loaded_bitstream_identity": "not_proven_by_ltx"}
