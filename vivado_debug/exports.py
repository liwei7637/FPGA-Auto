"""Optional offline exports, separate from the generic hardware backend."""
from pathlib import Path

from .common import DebugError, digest, integer
from .plugins.mac_pcap import decode_csv, write_pcap


def export_mac_pcap(csv_path, output, data_column=None, control_column=None, tick_column=None,
                    starts_at_frame_boundary=False, period_ns=5):
    output = Path(output).resolve()
    sidecar = output.with_suffix(output.suffix + ".json")
    if output.exists() or sidecar.exists():
        raise DebugError("output_exists", "PCAP/report already exists; select new paths")
    period_ns = integer(period_ns, "period_ns", 1, 1000000000)
    packets, report = decode_csv(Path(csv_path), data_column, control_column, tick_column,
                                 starts_at_frame_boundary, period_ns)
    report.update(source_kind="offline_capture", source_sha256=digest(csv_path), board_result_update=False)
    write_pcap(output, packets)
    import json
    with sidecar.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return {"pcap": str(output), "sha256": digest(output), "report": str(sidecar), "summary": report}
