"""Synthetic report/artifact fixture; never a real Vivado or deployable bitstream."""
import json
import os
from pathlib import Path
import sys
import time

TIMING = """Tool Version : Vivado v.2020.2
Design Timing Summary
WNS(ns) TNS(ns) TNS Failing Endpoints TNS Total Endpoints WHS(ns) THS(ns) THS Failing Endpoints THS Total Endpoints WPWS(ns) TPWS(ns) TPWS Failing Endpoints TPWS Total Endpoints
1.0 0.0 0 8 0.1 0.0 0 8 1.0 0.0 0 8
All user specified timing constraints are met.
Clock Summary
"""
DRC = """Tool Version : Vivado v.2020.2
Report DRC
1. REPORT SUMMARY
-----------------
No DRC violations found
2. REPORT DETAILS
"""


def main():
    if "-version" in sys.argv:
        print("Vivado v2020.2 (SYNTHETIC BITSTREAM TEST)")
        return
    directory = Path(os.environ["FPGA_JOB_DIR"])
    root = Path(os.environ["FPGA_PROJECT"]).parent
    options = json.loads((root / "control.json").read_text()) if (root / "control.json").exists() else {}
    operation = os.environ["FPGA_OPERATION"]
    if options.get("wakeup_verification") and operation != "write_bit":
        from verify_desktop_wakeup import begin_test_stage
        begin_test_stage(Path(options["wakeup_verification"]))
    bridge = dict(stage="bitstream" if operation == "write_bit" else "synthesis", done="false",
                  checked="true", synthesis_status="passed", implementation_status="passed")
    (directory / "bridge.json").write_text(json.dumps(bridge))
    time.sleep(options.get("bit_delay", .1) if operation == "write_bit" else options.get("delay", .1))
    if operation == "write_bit":
        artifact = Path(os.environ["FPGA_BITSTREAM_OUTPUT"])
        artifact.write_bytes(b"SYNTHETIC TEST ARTIFACT - NOT A REAL FPGA BITSTREAM")
        bridge.update(bitstream_status="passed", bitstream_path=str(artifact))
    else:
        (directory / "reports/timing_summary.rpt").write_text(TIMING)
        (directory / "reports/drc.rpt").write_text(DRC)
        bridge.update(stage="post_route", timing_complete="true", drc_complete="true")
    bridge["done"] = "true"
    (directory / "bridge.json").write_text(json.dumps(bridge))


if __name__ == "__main__":
    main()
