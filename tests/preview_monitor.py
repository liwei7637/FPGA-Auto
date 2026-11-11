"""Visible monitor demo using synthetic records only; never invokes Vivado."""
import argparse
from pathlib import Path
import json
import time

from fpga_auto.jobs import Job, atomic_json
from fpga_auto.monitor import launch
from fpga_auto.results import new_result


def main():
    parser = argparse.ArgumentParser(description="Preview progress and logs with clearly labelled simulated data")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seconds-per-stage", type=int, default=12)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    job = Job(root / "fpga.json", "build")
    job.request.update(project_path=str(root / "DEMO_演示工程.xpr"), monitor_demo=True)
    job.save_request()
    job.update(state="running")
    info = launch(job.directory, open_browser=not args.no_open)
    print(json.dumps(dict(info, config=str(root / "fpga.json")), ensure_ascii=False), flush=True)
    bridge = {}
    phases = [("check", "检查工程与源文件", "checked"),
              ("synthesis", "综合逻辑与优化网表", "synthesis_status"),
              ("implementation", "布局 / 布线", "implementation_status"),
              ("timing", "生成时序报告", "timing_complete"),
              ("drc", "设计规则检查", "drc_complete")]
    for stage, label, flag in phases:
        bridge.update(stage=stage)
        job.update(stage=stage)
        log = job.log
        if stage in ("synthesis", "implementation"):
            run_dir = job.directory / "demo_runs" / stage
            run_dir.mkdir(parents=True)
            log = run_dir / "runme.log"
            bridge.update({stage + "_directory": str(run_dir), flag: "running"})
        atomic_json(job.directory / "bridge.json", bridge)
        for index in range(args.seconds_per_stage):
            with log.open("a", encoding="utf-8") as stream:
                stream.write(time.strftime("[%H:%M:%S] ") + "INFO: [演示] {} · 输出 {}\n".format(label, index + 1))
                if index == 3 and stage == "synthesis":
                    stream.write("WARNING: [演示] 示例警告，用于检查筛选和高亮显示。\n")
            time.sleep(1)
        bridge[flag] = "passed" if flag.endswith("_status") else "true"
        atomic_json(job.directory / "bridge.json", bridge)
        if log != job.log:
            (job.directory / "reports" / (stage + "_run.log")).write_bytes(log.read_bytes())
    result = new_result("build", job.id)
    result.update(execution="completed", acceptance="passed", stage="post_route")
    job.finish(result)
    print("Monitor demo completed; no real Vivado build was run.", flush=True)


if __name__ == "__main__":
    main()
