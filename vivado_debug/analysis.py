"""Offline evidence analysis. Never infer a board PASS from missing data."""
from __future__ import annotations

import csv
from pathlib import Path
import re

from .common import DebugError, digest, integer

COUNTERS = ["tx_packets", "tx_payload_bytes", "rx_packets", "rx_good_packets",
            "rx_bad_packets", "rx_payload_bytes", "sequence_gaps", "late_or_duplicate",
            "data_errors", "length_keep_errors", "header_errors", "ignored_udp",
            "tuser_errors", "rx_aborts", "tx_aborts", "tx_backpressure_cycles",
            "elapsed_cycles", "mac_rx_frames", "mac_tx_frames", "ready_falls",
            "rx_bad_frame_events", "tx_bad_frame_events", "rx_overflow_events", "tx_overflow_events",
            "first_error_sequence", "first_error_offset", "next_expected_sequence", "snapshot_id",
            "rx_good_payload_bytes", "reserved29", "reserved30", "reserved31"]
ERROR_INDICES = (4, 6, 7, 8, 9, 10, 12, 13, 14, 19, 20, 21, 22, 23)


def make_snapshot(values, status, **metadata):
    if len(values) != 32:
        raise DebugError("snapshot_invalid", "Expected all 32 frozen counter slots")
    slots = [integer(v, "counter") for v in values]
    status = integer(status, "status", 0, 0xffffffff)
    if not status & 0x100:
        raise DebugError("snapshot_invalid", "Hardware snapshot_valid is not asserted")
    return {"values": [str(v) for v in slots], "status": f"0x{status:08x}",
            "snapshot_id": str(slots[27]), **metadata}


def read_snapshots(path):
    groups, pages, statuses = {}, {}, {}
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            for line, row in enumerate(csv.reader(stream, delimiter="\t"), 1):
                if not row:
                    continue
                if len(row) != 8:
                    raise ValueError(f"Line {line}: expected eight TSV fields")
                key = (row[0], row[1])
                if row[2] == "status":
                    if key in statuses:
                        raise ValueError("Duplicate status record")
                    statuses[key] = (int(row[3], 16), row[4:])
                    continue
                page = integer(row[2], "page", 0, 7)
                page_map = pages.setdefault(key, {})
                if page in page_map:
                    raise ValueError("Duplicate snapshot page")
                values = [integer(x, "slot") for x in row[3:7]]
                raw = int(row[7], 16)
                if raw != sum(v << (64 * i) for i, v in enumerate(values)):
                    raise ValueError("Raw 256-bit page does not match decoded slots")
                page_map[page] = values
        for key, page_map in pages.items():
            if set(page_map) != set(range(8)) or key not in statuses:
                raise ValueError("Incomplete snapshot pages/status: " + str(key))
            status, identity = statuses[key]
            sample = make_snapshot([v for p in range(8) for v in page_map[p]], status,
                                   mac=identity[0], ip=identity[1], source=str(Path(path).resolve()))
            groups.setdefault(key[0], {})[key[1]] = sample
        if set(statuses) != set(pages) or not groups:
            raise ValueError("Missing snapshot pages")
        return groups
    except (OSError, ValueError, IndexError, DebugError) as exc:
        raise DebugError("capture_invalid", str(exc)) from exc


def check_pair(samples, payload_bytes, expected_tx=None):
    """Compare the final frozen totals, including otherwise invisible tail loss."""
    payload_bytes = integer(payload_bytes, "payload_bytes", 32, 1472)
    if len(samples) != 2:
        raise DebugError("snapshot_invalid", "Two endpoint snapshots are required")
    endpoints = list(samples)
    reasons = []
    normalized = {}
    for endpoint, sample in samples.items():
        normalized[endpoint] = make_snapshot(sample["values"], sample["status"])
    values = {k: [int(v) for v in s["values"]] for k, s in normalized.items()}
    for index, endpoint in enumerate(endpoints):
        peer = endpoints[1 - index]
        v, pv = values[endpoint], values[peer]
        if expected_tx is not None and v[0] != integer(expected_tx[endpoint], "expected tx"):
            reasons.append(endpoint + ": tx_packet_target")
        for field, actual, expected in (("tx_bytes", v[1], v[0] * payload_bytes),
                                         ("rx_packets", v[2], pv[0]),
                                         ("rx_good_packets", v[3], pv[0]),
                                         ("rx_good_bytes", v[28], pv[0] * payload_bytes)):
            if actual != expected:
                reasons.append(f"{endpoint}: {field}={actual}, expected={expected}")
        for slot in ERROR_INDICES:
            if v[slot]:
                reasons.append(f"{endpoint}: {COUNTERS[slot]}={v[slot]}")
        status = int(normalized[endpoint]["status"], 16)
        if status & 0xf230 or not status & 4:
            reasons.append(endpoint + ": final_status=" + normalized[endpoint]["status"])
    if not any(v[0] for v in values.values()):
        reasons.append("no_test_packets")
    return {"measurement_validity": "valid", "verdict": "FAIL" if reasons else "PASS",
            "reasons": reasons, "scope": "selected UDP stream; not wire rate, FCS or BER",
            "totals": {k: {name: str(v[i]) for i, name in enumerate(COUNTERS)} for k, v in values.items()}}


def throughput(begin, end, clock_hz=200000000):
    begin = make_snapshot(begin["values"], begin["status"])
    end = make_snapshot(end["values"], end["status"])
    b, e = [int(v) for v in begin["values"]], [int(v) for v in end["values"]]
    if e[27] <= b[27] or any(e[i] < b[i] for i in (16, 28, 3)):
        raise DebugError("measurement_interrupted", "Snapshot ID or counters regressed/repeated")
    if any((int(s["status"], 16) & 0x34) != 0x34 for s in (begin, end)):
        raise DebugError("measurement_invalid", "Both interval boundaries must be running and ready")
    cycles, count = e[16] - b[16], e[28] - b[28]
    if cycles <= 0 or count <= 0 or any(e[i] != b[i] for i in (13, 14, 19)):
        raise DebugError("measurement_interrupted", "Empty interval or observed reset/link interruption")
    hz = integer(clock_hz, "clock_hz", 1, 10**10)
    return {"cycles": str(cycles), "rx_good_payload_bytes": str(count),
            "rx_good_packets": str(e[3] - b[3]), "udp_payload_gbps": count * 8 * hz / cycles / 1e9,
            "clock_hz": str(hz), "timebase": "nominal FPGA clock; not calibrated",
            "cross_device_synchronized": False}


def analyze_udp(path, tag="final", payload_bytes=1472, expected_tx=None, begin_tag=None, end_tag=None):
    try:
        groups = read_snapshots(path)
        if tag not in groups:
            raise DebugError("capture_invalid", "Snapshot tag absent: " + tag)
        result = check_pair(groups[tag], payload_bytes, expected_tx)
        if (begin_tag is None) != (end_tag is None):
            raise DebugError("invalid_argument", "Supply both interval tags")
        if begin_tag:
            result["throughput"] = {ep: throughput(groups[begin_tag][ep], groups[end_tag][ep]) for ep in groups[tag]}
        return {"source_kind": "historical_replay", "source_sha256": digest(path), **result,
                "board_result_update": False}
    except (DebugError, KeyError) as exc:
        return {"source_kind": "historical_replay", "measurement_validity": "invalid",
                "verdict": "INCONCLUSIVE", "reason": getattr(exc, "reason", "capture_invalid"),
                "message": str(exc), "board_result_update": False}


def analyze_ila_csv(path):
    """Describe exactly the exported window; constant samples are not continuity proof."""
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            headers = next(reader)
            if len(set(headers)) != len(headers) or not headers:
                raise ValueError("Duplicate/empty column names")
            stats = [{"first": None, "last": None, "changes": 0, "unknown": 0} for _ in headers]
            count = 0
            for row in reader:
                if not row or not any(row):
                    continue
                if row[0].strip().lower().startswith("radix"):
                    continue
                if len(row) != len(headers):
                    raise ValueError("Truncated or overlong CSV row")
                for stat, value in zip(stats, row):
                    if stat["first"] is None:
                        stat["first"] = value
                    elif stat["last"] != value:
                        stat["changes"] += 1
                    stat["last"] = value
                    if re.search(r"[xz?]", value.lower().removeprefix("0x")):
                        stat["unknown"] += 1
                count += 1
            if not count:
                raise ValueError("No samples")
        return {"source_kind": "offline_capture", "source_sha256": digest(path), "rows": count,
                "measurement_validity": "valid" if not any(x["unknown"] for x in stats) else "invalid",
                "columns": dict(zip(headers, stats)), "verdict": "INCONCLUSIVE",
                "scope": "exported window only; no implied board PASS or long-term continuity"}
    except (OSError, ValueError, StopIteration) as exc:
        raise DebugError("capture_invalid", str(exc)) from exc
