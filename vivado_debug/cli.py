"""Vivado Debug CLI: defaults to offline/mock, no implicit hardware discovery."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__
from .analysis import analyze_ila_csv, analyze_udp
from .common import DebugError, atomic_json, error_record, json_safe
from .ltx import describe
from .profile import PACKAGE, load_profile
from .sessions import OPERATIONS, SessionClient


def main(argv=None):
    parser = argparse.ArgumentParser(description=f"Vivado Debug {__version__} / Vivado 2020.2 / offline by default")
    parser.add_argument("--root", default=str(PACKAGE.parent))
    parser.add_argument("--enable-hardware", action="store_true", help="Explicitly enable real hardware for this invocation")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info")
    command = sub.add_parser("ltx")
    command.add_argument("path")
    command = sub.add_parser("profile-from-ltx")
    command.add_argument("path")
    command.add_argument("output")
    command.add_argument("--part", help="Explicit short PART for later hardware binding, e.g. xcku040")
    command = sub.add_parser("open")
    command.add_argument("--backend", choices=("mock", "vivado"), default="mock")
    command.add_argument("--profile")
    command.add_argument("--machine")
    command.add_argument("--ltx")
    command.add_argument("--vivado")
    command.add_argument("--scenario", default="normal")
    command.add_argument("--idle-seconds", type=float, default=900)
    command = sub.add_parser("session")
    command.add_argument("id")
    command = sub.add_parser("submit")
    command.add_argument("id")
    command.add_argument("operation", choices=OPERATIONS)
    command.add_argument("--params", default="{}", help="JSON object; use --params-file for multiline values")
    command.add_argument("--params-file")
    command.add_argument("--notify-thread")
    command.add_argument("--timeout", type=float, default=120)
    for name in ("job", "cancel", "wait"):
        command = sub.add_parser(name)
        command.add_argument("id")
        if name == "wait":
            command.add_argument("--timeout", type=float, default=30)
    command = sub.add_parser("analyze-udp")
    command.add_argument("path")
    command.add_argument("--tag", default="final")
    command.add_argument("--payload", type=int, default=1472)
    command.add_argument("--begin-tag")
    command.add_argument("--end-tag")
    command = sub.add_parser("analyze-ila")
    command.add_argument("path")
    args = parser.parse_args(argv)
    client = SessionClient(args.root, enable_hardware=args.enable_hardware)
    try:
        if args.command == "info":
            result = {"name": "Vivado Debug MCP", "version": __version__, "vivado_version": "2020.2",
                      "default_backend": "mock", "hardware_enabled": args.enable_hardware,
                      "hardware_validated": False, "hardware_validation_scope": "single-device ILA/VIO, timeout recovery and completion notification PASS; dual-device UDP PENDING",
                      "build_cli": "fpga.cmd (unchanged)"}
        elif args.command == "ltx":
            result = describe(args.path)
        elif args.command == "profile-from-ltx":
            if Path(args.output).exists():
                raise DebugError("output_exists", "Preserve existing profile")
            layout = describe(args.path)
            result = {"schema_version": 1, "id": "custom-debug", "vivado_version": "2020.2",
                      "cores": {"core" + str(i): core for i, core in enumerate(layout["cores"])}}
            if args.part:
                result["part"] = args.part
            atomic_json(args.output, result)
        elif args.command == "open":
            result = client.open(args.profile, args.machine, args.backend, args.ltx, args.vivado, args.scenario, args.idle_seconds)
        elif args.command == "session":
            result = client.get(args.id)
        elif args.command == "submit":
            params = json.loads(Path(args.params_file).read_text(encoding="utf-8-sig") if args.params_file else args.params)
            result = client.submit(args.id, args.operation, params, args.notify_thread, args.timeout)
        elif args.command == "job":
            result = client.job_get(args.id)
        elif args.command == "cancel":
            result = client.cancel(args.id)
        elif args.command == "wait":
            result = client.wait(args.id, args.timeout)
        elif args.command == "analyze-ila":
            result = analyze_ila_csv(args.path)
        else:
            result = analyze_udp(args.path, args.tag, args.payload, begin_tag=args.begin_tag, end_tag=args.end_tag)
        print(json.dumps(json_safe(result), ensure_ascii=False, indent=2))
        if result.get("execution") == "failed" or result.get("state") == "failed":
            return 130 if result.get("reason") == "cancelled" else 1
        verdict = result.get("debug", result).get("verdict")
        return 2 if verdict == "FAIL" else 0
    except Exception as exc:
        print(json.dumps(error_record(exc), ensure_ascii=False, indent=2))
        return 1
