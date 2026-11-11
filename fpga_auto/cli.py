"""Command-line entry point."""
import argparse
import json
import math
import os
import sys
from pathlib import Path
import time
import traceback
from .config import load_config
from .results import FPGAError, new_result, exit_code, merge_run_status, apply_acceptance
from .vivado import find_vivado, verify_version
from .jobs import Job, ProjectLock, job_state, locate_job, atomic_json, now
from .runner import read_bridge, run_vivado
from . import __version__


def make_parser():
    parser = argparse.ArgumentParser(description="Vivado 2020.2 standard project CLI")
    parser.add_argument("--version", action="version", version="FPGA Auto " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "synth", "quick", "impl", "build", "timing", "drc", "bit", "status", "logs"):
        command = commands.add_parser(name)
        command.add_argument("--config", default="fpga.json")
        command.add_argument("--json", action="store_true", dest="json_output")
        if name not in ("status", "logs"):
            command.add_argument("--vivado", help="Vivado executable or installation directory")
            command.add_argument("--timeout", type=float, default=14400, help="Wall time limit in seconds")
            command.add_argument("--no-monitor", action="store_true", help="Do not open the local progress/log dashboard")
        else:
            command.add_argument("--job", help="Job ID; default: current or latest job")
        if name == "logs":
            command.add_argument("--follow", action="store_true")
        if name == "build":
            command.add_argument("--bit", action="store_true", help="Generate bitstream only after Timing/DRC acceptance passes")
    return parser


def inspect_job(args):
    directory = locate_job(args.config, args.job)
    if args.command == "status":
        return job_state(directory)
    log_path = directory / "vivado.log"
    if args.follow:
        if args.json_output:
            raise FPGAError("arguments_invalid", "--follow cannot be combined with --json")
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            while True:
                chunk = stream.read(65536)
                if chunk:
                    print(chunk, end="", flush=True)
                    continue
                if job_state(directory)["state"] not in ("created", "running"):
                    break
                time.sleep(0.5)
    return dict(job_id=directory.name, log=str(log_path), reports=str(directory / "reports"))


def execute(args, job=None):
    result = new_result(args.command)
    bit_requested = args.command == "bit" or (args.command == "build" and getattr(args, "bit", False))
    if bit_requested:
        result["bitstream"] = {"status": "not_started"}
    cancelled = False
    started = time.monotonic()
    try:
        job = job or Job(args.config, args.command)
        result["job_id"] = job.id
        job.request["generate_bitstream"] = bit_requested
        job.save_request()
        if not job.request.get("background") and not getattr(args, "no_monitor", True):
            from .monitor import launch
            result["monitor"] = launch(job.directory)
        job.check_cancelled()
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise FPGAError("arguments_invalid", "--timeout must be positive and finite")
        if os.name != "nt":
            raise FPGAError("platform_unsupported", "V1 supports Windows only")
        config = load_config(args.config)
        if job.request.get("background") and any(
                str(config[key]) != str(job.request.get(key))
                for key in ("project_path", "synth_run", "impl_run", "jobs", "vivado_version")):
            raise FPGAError("config_changed", "Configuration changed after submission; no build started")
        result.update(project=str(config["project_path"]), vivado_version=config["vivado_version"],
                      synth_run=config["synth_run"], impl_run=config["impl_run"])
        job.request.update({k: str(v) if isinstance(v, Path) else v for k, v in config.items()})
        job.request["timeout_seconds"] = args.timeout
        job.save_request()
        with ProjectLock(config["project_path"], job) as lock:
            executable = find_vivado(args.vivado)
            job.request["vivado_executable"] = str(executable)
            job.save_request()
            verify_version(executable, job.directory / "version.log", min(60, args.timeout))
            script = Path(__file__).resolve().parent.parent / "tcl" / (args.command + ".tcl")
            if not script.is_file():
                raise FPGAError("not_implemented", args.command + " is pending its development phase")
            remaining = args.timeout - (time.monotonic() - started)
            job.check_cancelled()
            if remaining <= 0:
                raise FPGAError("timeout", "Timeout exhausted during preflight")
            code, bridge = run_vivado(executable, script, config, job, lock, remaining)
            result["stage"] = bridge.get("stage", job.state["stage"])
            merge_run_status(result, bridge)
            if code or bridge.get("done") != "true":
                raise FPGAError(bridge.get("reason", "vivado_execution_failed"),
                                bridge.get("message", "Vivado returned {} without a valid completion record".format(code)),
                                result["stage"])
            apply_acceptance(result, job.directory / "reports")
            if bit_requested:
                if result["acceptance"] != "passed":
                    result["bitstream"] = dict(status="skipped", reason="acceptance_" + result["acceptance"])
                else:
                    from .bitstream import reserve_output, inspect_output
                    job.check_cancelled()
                    remaining = args.timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise FPGAError("timeout", "Timeout exhausted before bitstream generation", "bitstream")
                    output = reserve_output(job)
                    result["stage"] = "bitstream"
                    result["bitstream"] = {"status": "running"}
                    job.update(stage="bitstream")
                    script = script.with_name("write_bit.tcl")
                    code, bridge = run_vivado(executable, script, config, job, lock, remaining)
                    job.check_cancelled()
                    if code or bridge.get("done") != "true":
                        raise FPGAError(bridge.get("reason", "bitstream_failed"),
                                        bridge.get("message", "Bitstream generation did not complete"), "bitstream")
                    result["bitstream"] = inspect_output(job, bridge)
                    result["paths"]["bitstream"] = str(output)
            result["execution"] = "completed"
    except FPGAError as exc:
        cancelled = exc.reason == "cancelled"
        result.update(execution="failed", acceptance=result["acceptance"] if bit_requested else "unknown",
                      reason=exc.reason, message=str(exc), stage=exc.stage)
        if job and exc.stage == "preflight" and job.state["stage"] != "preflight":
            result["stage"] = job.state["stage"]
    except KeyboardInterrupt:
        cancelled = True
        result.update(execution="failed", acceptance="unknown", reason="cancelled", message="Interrupted by user",
                      stage=job.state["stage"] if job else "preflight")
    except Exception as exc:
        result.update(execution="failed", acceptance="unknown", reason="python_exception", message=str(exc),
                      stage=job.state["stage"] if job else "preflight")
        if job:
            (job.directory / "python_error.log").write_text(traceback.format_exc(), encoding="utf-8")
    if job:
        if bit_requested and result["execution"] != "completed":
            status = "cancelled" if cancelled else ("failed" if result["bitstream"]["status"] == "running" else "skipped")
            result["bitstream"] = dict(status=status, reason=result.get("reason"))
        bridge = read_bridge(job)
        merge_run_status(result, bridge)
        try:
            if result.get("reason"):
                with job.log.open("a", encoding="utf-8") as log:
                    log.write("\nFPGA_AUTO: {}: {}\n".format(result["reason"], result["message"]))
            job.finish(result, cancelled)
        except OSError as exc:
            if result.get("reason"):
                result["original_failure"] = dict(reason=result["reason"], message=result["message"])
            result.update(execution="failed", acceptance="unknown", reason="job_write_failed", message=str(exc))
            result.setdefault("start_time", job.state["start_time"])
            result["end_time"] = now()
            result["paths"].update(job=str(job.directory), log=str(job.log), reports=str(job.directory / "reports"))
            # Always return a failure envelope to stdout, even if persistent
            # storage is unavailable. Replace a previously written success where possible.
            try:
                atomic_json(job.directory / "result.json", result)
                job.update(state="failed", reason="job_write_failed", end_time=result["end_time"])
            except OSError:
                result["persistence_error"] = "Could not persist final failure record; stdout is authoritative"
    return result


def main(argv=None):
    args = make_parser().parse_args(argv)
    if args.json_output and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.command in ("status", "logs"):
        try:
            result = inspect_job(args)
            code = 0
        except (FPGAError, OSError, ValueError) as exc:
            result = dict(reason=getattr(exc, "reason", "job_read_failed"), message=str(exc))
            code = 1
        except KeyboardInterrupt:
            return 130
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    result = execute(args)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        label = "PASSED" if args.command == "check" and result["execution"] == "completed" else result["execution"].upper()
        print("{} {}".format(args.command.upper(), label))
        if args.command == "check" and result["execution"] == "completed":
            for label, key in (("Vivado", "vivado_version"), ("Project", "project"),
                               ("Synth", "synth_run"), ("Impl", "impl_run")):
                print("{}: {}".format(label, result[key]))
        if result["reason"]:
            print("{}: {}".format(result["reason"], result["message"]))
        print("Acceptance: " + result["acceptance"])
        if args.command != "check":
            for key in ("synthesis", "implementation", "timing", "drc"):
                print("{}: {}".format(key.upper(), result[key]["status"].upper()))
        if args.command in ("timing", "build", "bit"):
            for key in ("wns_ns", "tns_ns", "whs_ns", "ths_ns"):
                value = result["timing"][key]
                print("{}: {}".format(key[:-3].upper(), "unknown" if value is None else "{:+.3f} ns".format(value)))
            print("Setup failing endpoints: {}".format(result["timing"]["failing_endpoints"]))
        if args.command in ("drc", "build", "bit"):
            print("DRC errors / critical warnings / warnings: {} / {} / {}".format(
                result["drc"]["errors"], result["drc"]["critical_warnings"], result["drc"]["warnings"]))
        print("Job: " + result["paths"].get("job", "unavailable"))
        if result["bitstream"]["status"] != "not_requested":
            print("Bitstream: " + result["bitstream"]["status"])
            if result["bitstream"].get("path"):
                print("Artifact: " + result["bitstream"]["path"])
        if "monitor" in result:
            print("Monitor: " + result["monitor"].get("url", result["monitor"].get("warning", "unavailable")))
    return exit_code(result)
