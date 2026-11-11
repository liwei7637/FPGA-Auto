"""Convert the existing MAC RX/TX ILA CSV to nanosecond Ethernet PCAP.

Only valid && ready beats are assembled. Byte lane zero is first on the wire.
Default synchronization discards through the first TLAST, because an ILA
window can start inside a frame. No preamble or FCS is present at these taps.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import struct
from pathlib import Path


def number(value: str, radix: int = 16) -> int:
    value = value.strip().replace("_", "")
    match = re.fullmatch(r"(?:\d+)?'([hHbBdD])([0-9a-fA-F]+)", value)
    if match:
        return int(match[2], {"h": 16, "b": 2, "d": 10}[match[1].lower()])
    if re.search(r"[xXzZ?]", value.replace("0x", "").replace("0X", "")):
        raise ValueError(f"Unknown value in a capture field: {value!r}")
    return int(value, 0 if value.lower().startswith(("0x", "0b")) else radix)


def column(headers: list[str], selector: str | None, candidates: tuple[str, ...]) -> int:
    if selector is not None:
        if selector.isdigit():
            index = int(selector)
            if index >= len(headers):
                raise ValueError(f"Column {index} is out of range")
            return index
        matches = [index for index, name in enumerate(headers) if name == selector]
    else:
        matches = [index for index, name in enumerate(headers)
                   if any(candidate.lower() in name.lower() for candidate in candidates)]
    if len(matches) != 1:
        raise ValueError(f"Cannot uniquely select {selector or candidates}; use --list-columns and an explicit column")
    return matches[0]


def decode_csv(path: Path, data_column: str | None = None, control_column: str | None = None,
               tick_column: str | None = None, starts_at_frame_boundary: bool = False,
               period_ns: int = 5) -> tuple[list[tuple[int, bytes, bool]], dict]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.reader(source)
        headers = next(reader)
        data_index = column(headers, data_column, ("probe0", "mac_rx_axis_tdata", "mac_tx_axis_tdata"))
        control_index = column(headers, control_column, ("probe2",))
        tick_index = column(headers, tick_column, ("probe6", "debug_tick_q"))
        radices = {data_index: 16, control_index: 16, tick_index: 16}
        previous_tick = None
        tick_epoch = 0
        synchronized = starts_at_frame_boundary
        frame = bytearray()
        frame_tick = 0
        packets = []
        report = {"source": str(path.resolve()), "columns": {
            "data": headers[data_index], "control": headers[control_index], "tick": headers[tick_index]},
            "accepted_beats": 0, "discarded_prefix_beats": 0, "trailing_partial_bytes": 0,
            "packet_count": 0, "bad_mac_frames": [], "period_ns": period_ns,
            "time_origin": "FPGA tick relative to reconfiguration; not wall clock",
            "capture_boundary": "system-side MAC FIFO; no preamble/FCS; TX is not wire proof"}
        for row_number, row in enumerate(reader, start=2):
            if not row or not any(cell.strip() for cell in row):
                continue
            if row[0].strip().lower().startswith("radix"):
                for index in radices:
                    label = row[index].strip().upper()
                    if label in {"HEX", "HEXADECIMAL", "BINARY", "BIN", "UNSIGNED", "SIGNED", "DECIMAL", "DEC"}:
                        radices[index] = 16 if label.startswith("HEX") else 2 if label.startswith("BIN") else 10
                continue
            try:
                control = number(row[control_index], radices[control_index])
                tick = number(row[tick_index], radices[tick_index])
                if not 0 <= control < (1 << 24) or not 0 <= tick < (1 << 32):
                    raise ValueError("Control/tick exceeds the documented probe width")
                if previous_tick is not None and tick < previous_tick:
                    if previous_tick - tick < (1 << 31):
                        raise ValueError("Tick moved backwards; split concatenated windows before conversion")
                    tick_epoch += 1 << 32
                previous_tick = tick
                absolute_tick = tick_epoch + tick
                if (control >> 14) & 3 != 3:
                    continue
                report["accepted_beats"] += 1
                last = bool(control & (1 << 13))
                if not synchronized:
                    report["discarded_prefix_beats"] += 1
                    synchronized = last
                    continue
                keep = (control >> 16) & 255
                if not keep or (not last and keep != 255) or keep & (keep + 1):
                    raise ValueError(f"Invalid MAC TKEEP {keep:#x}")
                data = number(row[data_index], radices[data_index])
                if not 0 <= data < (1 << 64):
                    raise ValueError("Data exceeds 64 bits")
                if not frame:
                    frame_tick = absolute_tick
                # TKEEP was checked as contiguous low bits; bit_length also
                # supports the Python 3.8 baseline installed on this machine.
                frame.extend(data.to_bytes(8, "little")[:keep.bit_length()])
                if last:
                    if len(frame) < 14:
                        raise ValueError("Frame has no complete Ethernet header")
                    good = bool(control & (1 << 12))
                    packets.append((frame_tick * period_ns, bytes(frame), good))
                    if not good:
                        report["bad_mac_frames"].append(len(packets))
                    frame.clear()
            except (IndexError, ValueError) as error:
                raise ValueError(f"CSV row {row_number}: {error}") from error
        report["packet_count"] = len(packets)
        report["trailing_partial_bytes"] = len(frame)
        return packets, report


def write_pcap(path: Path, packets: list[tuple[int, bytes, bool]]) -> None:
    with path.open("xb") as output:
        # Little-endian nanosecond PCAP 2.4, LINKTYPE_ETHERNET=1.
        output.write(struct.pack("<IHHIIII", 0xA1B23C4D, 2, 4, 0, 0, 262144, 1))
        for timestamp_ns, frame, _good in packets:
            seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
            output.write(struct.pack("<IIII", seconds, nanoseconds, len(frame), len(frame)))
            output.write(frame)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--data-column", help="Exact CSV header or zero-based column index")
    parser.add_argument("--control-column", help="24-bit MAC control probe, not individual flags")
    parser.add_argument("--tick-column", help="32-bit 200 MHz debug tick probe")
    parser.add_argument("--period-ns", type=int, default=5)
    parser.add_argument("--starts-at-frame-boundary", action="store_true")
    parser.add_argument("--list-columns", action="store_true")
    args = parser.parse_args()
    if args.list_columns:
        with args.csv.open(encoding="utf-8-sig", newline="") as source:
            for index, name in enumerate(next(csv.reader(source))):
                print(f"{index}: {name}")
        return
    if args.output is None or args.period_ns <= 0:
        parser.error("--output and a positive --period-ns are required")
    report_path = args.output.with_suffix(args.output.suffix + ".json")
    if args.output.exists() or report_path.exists():
        parser.error("Output/report already exists; choose new paths")
    packets, report = decode_csv(args.csv, args.data_column, args.control_column, args.tick_column,
                                 args.starts_at_frame_boundary, args.period_ns)
    write_pcap(args.output, packets)
    with report_path.open("x", encoding="utf-8") as destination:
        json.dump(report, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
