"""Run isolated Vivado command/Tcl checks. Does not open a project or hardware."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fpga_auto.runner import run_process
from fpga_auto.vivado import command_line
from vivado_debug.common import atomic_json, digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vivado", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    script = Path(__file__).with_suffix(".tcl")
    log = output / "vivado.log"
    code = run_process(command_line(args.vivado, ["-mode", "batch", "-notrace", "-nojournal", "-nolog", "-source", str(script)]),
                       log, output, 90)
    text = log.read_text(encoding="utf-8", errors="replace")
    report = next((json.loads(line.split(" ", 1)[1]) for line in text.splitlines() if line.startswith("VDBG_OFFLINE_TEST ")), None)
    caps = next((json.loads(line.split(" ", 1)[1]) for line in text.splitlines() if line.startswith("VDBG_CAPABILITIES ")), None)
    passed = code == 0 and report is not None and report["passed"] and caps and not caps["missing_commands"]
    result = {"passed": bool(passed), "scope": "real Vivado 2020.2 command loading and stubbed Tcl bridge only",
              "hardware_connected": False, "project_opened": False, "process_returncode": code,
              "capabilities": caps, "checks": report, "log_sha256": digest(log)}
    atomic_json(output / "result.json", result)
    print(json.dumps(result, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
