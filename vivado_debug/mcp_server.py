"""Standalone MCP surface for generic Vivado ILA/VIO debugging."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from . import __version__
from .analysis import analyze_ila_csv, analyze_udp
from .common import error_record, json_safe
from .ltx import describe
from .exports import export_mac_pcap
from .profile import PACKAGE, load_profile
from .sessions import SessionClient

INSTRUCTIONS = (
    "Vivado Debug MCP targets Vivado 2020.2. Default sessions are SIMULATED, never board evidence. "
    "Real hardware needs explicit server enablement and exact target/DNA/LTX bindings. "
    "VIO writes, toggle commands, snapshots and ILA arming change state; do not replay uncertain writes. "
    "Save job/session IDs. Long jobs run in detached workers; use completion events, no model polling. "
    "A notification receiver must be the current explicitly supplied chat UUID, never a startup/recent chat. "
    "Session close does not imply stopped external hardware."
)


def create_server(root, enable_hardware=False):
    server = MCPServer("vivado-debug", title="Vivado ILA/VIO Debug", version=__version__,
                       instructions=INSTRUCTIONS, log_level="WARNING")
    client = SessionClient(root, enable_hardware=enable_hardware)
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    changes = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

    def guarded(function, *args, **kwargs):
        try:
            return json_safe(function(*args, **kwargs))
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(json.dumps(error_record(exc), ensure_ascii=False)) from exc

    def short(session_id, operation, params=None):
        submitted = client.submit(session_id, operation, params)
        # Eight-page hardware snapshots can exceed 40 s over JTAG. Wait for
        # the worker's 120 s operation deadline and its terminal record.
        result = client.wait(submitted["job_id"], 130)
        if result["execution"] != "completed":
            raise ToolError(json.dumps(result, ensure_ascii=False))
        return result

    @server.tool(annotations=read_only)
    def vivado_debug_info() -> dict[str, Any]:
        """Describe this standalone debug service and its offline/hardware boundary."""
        return {"name": "Vivado Debug MCP", "version": __version__, "vivado_version": "2020.2",
                "hardware_enabled": enable_hardware, "default_backend": "mock",
                "hardware_validation": "SINGLE_DEVICE_ILA_VIO_PASS_DUAL_UDP_PENDING",
                "hardware_validation_run": "RUN-20261106-VIVADO-MCP-03",
                "timeout_stop_recovery_validation": "PASS",
                "completion_notification_validation": "RECEIVED_AND_ACKNOWLEDGED",
                "build_and_notification_flow": "FPGA Auto retained"}

    @server.tool(annotations=read_only)
    def vivado_profile_describe(profile_path: str | None = None) -> dict[str, Any]:
        """Read project debug-core/physical-port semantics without launching Vivado."""
        return guarded(load_profile, profile_path)

    @server.tool(annotations=read_only)
    def vivado_ltx_describe(path: str) -> dict[str, Any]:
        """Read a Vivado JSON LTX layout and SHA-256; UUIDs do not prove loaded firmware identity."""
        return guarded(describe, path)

    @server.tool(annotations=changes)
    def vivado_session_open(profile_path: str | None = None, machine_path: str | None = None,
                            backend: str = "mock", ltx_path: str | None = None,
                            vivado: str | None = None, mock_scenario: str = "normal",
                            idle_seconds: float = 900) -> dict[str, Any]:
        """Open a persistent debug worker. Default is simulated. Hardware requires explicit server enablement."""
        return guarded(client.open, profile_path, machine_path, backend, ltx_path, vivado, mock_scenario, idle_seconds)

    @server.tool(annotations=read_only)
    def vivado_session_get(session_id: str) -> dict[str, Any]:
        """Read one explicitly identified session record; does not contact hardware."""
        return guarded(client.get, session_id)

    @server.tool(annotations=changes)
    def vivado_session_close(session_id: str) -> dict[str, Any]:
        """Close this service's session/resources. This does not assert all hardware traffic has stopped."""
        return guarded(short, session_id, "close")

    @server.tool(annotations=changes)
    def vivado_hw_status(session_id: str) -> dict[str, Any]:
        """Read bound FPGA configuration status/DNA and persist a traceable debug job."""
        return guarded(short, session_id, "hw_status")

    @server.tool(annotations=changes)
    def vivado_vio_read(session_id: str, endpoint: str, core: str, port: str) -> dict[str, Any]:
        """Read an inN/outN VIO physical port with exact hex/decimal strings and persist its job."""
        return guarded(short, session_id, "vio_read", {"endpoint": endpoint, "core": core, "port": port})

    @server.tool(annotations=changes)
    def vivado_vio_apply(session_id: str, endpoint: str, core: str, writes: dict[str, str]) -> dict[str, Any]:
        """Stage validated outN values, commit once and confirm readback. Values are decimal or 0x hex strings."""
        return guarded(short, session_id, "vio_apply", {"endpoint": endpoint, "core": core, "writes": writes})

    @server.tool(annotations=changes)
    def vivado_udp_snapshot(session_id: str, endpoint: str) -> dict[str, Any]:
        """Switch-UDP plugin: toggle SNAPSHOT and read all eight frozen pages. Changes VIO state."""
        return guarded(short, session_id, "snapshot", {"endpoint": endpoint})

    @server.tool(annotations=changes)
    def vivado_ila_capture_start(session_id: str, endpoint: str, core: str, depth: int = 1024,
                                 position: int = 0, triggers: dict[str, str] | None = None,
                                 immediate: bool = True, timeout: float = 30,
                                 notify_thread: str | None = None) -> dict[str, Any]:
        """Submit bounded ILA capture. Returns job ID; worker owns waiting and raw ILA/CSV export. No polling."""
        return guarded(client.submit, session_id, "ila_capture",
                       {"endpoint": endpoint, "core": core, "depth": depth, "position": position,
                        "triggers": triggers or {}, "immediate": immediate, "timeout": timeout},
                       notify_thread, timeout + 90)

    @server.tool(annotations=changes)
    def vivado_recipe_start(session_id: str, recipe: str = "switch_udp_v1",
                            parameters: dict[str, Any] | None = None,
                            notify_thread: str | None = None, timeout: float = 120) -> dict[str, Any]:
        """Submit the symmetric dual-FPGA UDP recipe. Workers configure/start/stop/drain/compare; mock is simulated."""
        if recipe != "switch_udp_v1":
            raise ToolError("Unknown recipe; available: switch_udp_v1")
        return guarded(client.submit, session_id, "recipe", parameters or {}, notify_thread, timeout)

    @server.tool(annotations=read_only)
    def vivado_job_get(job_id: str) -> dict[str, Any]:
        """Read a known job after completion or on user request; never schedule periodic model queries."""
        return guarded(client.job_get, job_id)

    @server.tool(annotations=changes)
    def vivado_job_cancel(job_id: str) -> dict[str, Any]:
        """Request cancellation. Check terminal cleanup evidence before claiming hardware stopped."""
        return guarded(client.cancel, job_id)

    @server.tool(annotations=read_only)
    def vivado_capture_analyze(path: str, format: str = "ila_csv", tag: str = "final",
                               payload_bytes: int = 1472, begin_tag: str | None = None,
                               end_tag: str | None = None) -> dict[str, Any]:
        """Offline ILA CSV summary or switch-UDP snapshots replay; no hardware access or board-result updates."""
        if format == "ila_csv":
            return guarded(analyze_ila_csv, path)
        if format == "udp_snapshots":
            return guarded(analyze_udp, path, tag, payload_bytes, begin_tag=begin_tag, end_tag=end_tag)
        raise ToolError("format must be ila_csv or udp_snapshots")

    @server.tool(annotations=changes)
    def vivado_mac_to_pcap(csv_path: str, output: str, data_column: str | None = None,
                            control_column: str | None = None, tick_column: str | None = None,
                            starts_at_frame_boundary: bool = False, period_ns: int = 5) -> dict[str, Any]:
        """Offline Switch-UDP MAC CSV export. Counts only valid&&ready, discards partial edges, preserves existing outputs."""
        return guarded(export_mac_pcap, csv_path, output, data_column, control_column, tick_column,
                       starts_at_frame_boundary, period_ns)

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="Standalone Vivado ILA/VIO Debug MCP / offline by default")
    parser.add_argument("--root", default=str(PACKAGE.parent))
    parser.add_argument("--enable-hardware", action="store_true")
    parser.add_argument("--list-tools", action="store_true", help="Print tool schemas without running a server or contacting hardware")
    args = parser.parse_args(argv)
    server = create_server(args.root, args.enable_hardware)
    if args.list_tools:
        import asyncio
        print(json.dumps([t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in asyncio.run(server.list_tools())], indent=2))
    else:
        server.run(transport="stdio")
