"""Opt-in real Vivado regression on a NEW tiny project; no user XPR is opened."""
import argparse
import json
import os
from pathlib import Path
import tempfile

from fpga_auto.jobs import atomic_json, read_json
from fpga_auto.runner import run_process
from fpga_auto.vivado import command_line, find_vivado, verify_version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vivado", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--part", default="xcku060-ffva1156-2-i")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="vivado-step-regression-", dir=args.output.resolve()))
    (root / "reports").mkdir()
    (root / "top.v").write_text(
        "module top(input clk, input rst, output [7:0] led);\n"
        "reg [7:0] count; always @(posedge clk) begin\n"
        "if (rst) count <= 0; else count <= count + 1'b1; end\n"
        "assign led = count; endmodule\n", encoding="ascii")
    (root / "top.xdc").write_text("create_clock -period 10.000 [get_ports clk]\n", encoding="ascii")
    executable = find_vivado(args.vivado)
    verify_version(executable, root / "version.log")
    env = dict(os.environ, FPGA_STEP_TEST_ROOT=str(root), FPGA_STEP_TEST_PART=args.part,
               FPGA_PROJECT=str(root / "project" / "step_regression.xpr"),
               FPGA_SYNTH="synth_1", FPGA_IMPL="impl_1", FPGA_JOBS="1", FPGA_JOB_DIR=str(root))
    script = Path(__file__).with_suffix(".tcl").resolve()
    log = root / "vivado.log"
    print("Real Vivado step regression: " + str(root), flush=True)
    code = run_process(command_line(executable, ["-mode", "batch", "-notrace", "-nojournal", "-nolog",
                                                 "-source", str(script)]), log, root, args.timeout, env=env)
    result = read_json(root / "bridge.json") if (root / "bridge.json").is_file() else {}
    passed = code == 0 and result.get("regression_passed") == "true"
    report = dict(passed=passed, vivado_version="2020.2", part=args.part,
                  real_synthesis_and_implementation=True, production_project_used=False,
                  bitstream_generated=any(root.rglob("*.bit")), returncode=code,
                  evidence=str(root), log=str(log), result=result)
    if report["bitstream_generated"]:
        passed = report["passed"] = False
    atomic_json(root / "validation.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not passed:
        print(log.read_text(encoding="utf-8", errors="replace")[-8000:], flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
