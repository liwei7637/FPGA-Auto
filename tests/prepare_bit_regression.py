"""Create NEW isolated, fully constrained projects and simulate the test counter.

This only prepares projects. Use the normal CLI start build --bit on each config.
No board pinout is implied; never program the generated test artifacts on hardware.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from fpga_auto.jobs import atomic_json
from fpga_auto.runner import run_process
from fpga_auto.vivado import command_line, find_vivado, verify_version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vivado", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--part", default="xcku040-ffva1156-2-i")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="bit-regression-", dir=args.output.resolve()))
    executable = find_vivado(args.vivado)
    verify_version(executable, root / "version.log")
    fixtures = Path(__file__).with_name("bit_regression")
    configs = {}
    for mode in ("route-only", "post-route"):
        destination = root / mode
        destination.mkdir()
        for name in ("bit_regression_top.v", "bit_regression_counter.v"):
            shutil.copy2(fixtures / name, destination / name)
        configs[mode] = str(destination / "fpga.json")
        atomic_json(destination / "fpga.json", dict(vivado_version="2020.2", project="project/bit_regression.xpr",
                                                    synth_run="synth_1", impl_run="impl_1", jobs=1))
    sim = root / "simulation"
    sim.mkdir()
    commands = [
        ("xvlog", [str(fixtures / "bit_regression_counter.v"), str(fixtures / "bit_regression_tb.v")]),
        ("xelab", ["bit_regression_tb", "-s", "bit_regression_sim"]),
        ("xsim", ["bit_regression_sim", "-runall"]),
    ]
    for program, arguments in commands:
        code = run_process(command_line(executable.parent / (program + ".bat"), arguments), sim / (program + ".log"), sim, 90)
        if code:
            raise RuntimeError(program + " failed; see " + str(sim))
    if "BIT_REGRESSION_SIMULATION_PASSED" not in (sim / "xsim.log").read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError("Counter simulation did not report success")
    script = Path(__file__).with_suffix(".tcl")
    env = dict(os.environ, FPGA_BIT_TEST_ROOT=str(root), FPGA_BIT_TEST_PART=args.part)
    code = run_process(command_line(executable, ["-mode", "batch", "-notrace", "-nojournal", "-nolog", "-source", str(script)]),
                       root / "prepare.log", root, 120, env=env)
    if code or "BIT_REGRESSION_PROJECTS_PREPARED" not in (root / "prepare.log").read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError("Preparation failed; see " + str(root / "prepare.log"))
    report = dict(root=str(root), configs=configs, simulation_passed=True, part=args.part, production_project_used=False)
    atomic_json(root / "prepared.json", report)
    atomic_json(args.output / "prepared-latest.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
