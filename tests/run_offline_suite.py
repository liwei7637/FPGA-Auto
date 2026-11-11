"""Separate stdlib-only build regression and opt-in MCP regression processes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    package = Path(__file__).resolve().parents[1]
    suites = []
    for name, directory in (("build-notification", "tests"), ("vivado-debug-mcp", "debug_tests")):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover",
                                 "-s", str(package / directory), "-v"], cwd=str(package),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", timeout=300)
        log = output / (name + ".log")
        log.write_text(result.stdout, encoding="utf-8")
        ran = re.search(r"Ran (\d+) tests? in", result.stdout)
        skipped = re.search(r"skipped=(\d+)", result.stdout)
        record = {"suite": name, "passed": result.returncode == 0,
                  "tests_run": int(ran[1]) if ran else 0, "skipped": int(skipped[1]) if skipped else 0,
                  "returncode": result.returncode, "log": log.name,
                  "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest()}
        suites.append(record)
        print(json.dumps(record), flush=True)
    report = {"passed": all(s["passed"] for s in suites), "suites": suites,
              "tests_run": sum(s["tests_run"] for s in suites), "skipped": sum(s["skipped"] for s in suites),
              "hardware_accessed": False, "process_isolation": "build stdlib / optional MCP SDK"}
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
