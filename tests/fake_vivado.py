"""Test-only process fixture, never an FPGA synthesis substitute."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if "-version" in sys.argv:
    print("Vivado v2020.2 (TEST FIXTURE)")
elif "--child" in sys.argv:
    time.sleep(60)
else:
    output = Path(os.environ["FPGA_JOB_DIR"])
    project = Path(os.environ["FPGA_PROJECT"])
    options = json.loads((project.parent / "control.json").read_text())
    if options.get("wakeup_verification"):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from verify_desktop_wakeup import begin_test_stage
        begin_test_stage(Path(options["wakeup_verification"]))
    if options.get("child"):
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--child"])
        (output / "child-pid.txt").write_text(str(child.pid))
    bridge = {"stage": "synthesis", "done": "false"}
    (output / "bridge.json").write_text(json.dumps(bridge))
    time.sleep(options.get("delay", 0.2))
    bridge.update(done="true", synthesis_status="passed", synthesis_run_status="synth_design Complete!")
    (output / "bridge.json").write_text(json.dumps(bridge))
