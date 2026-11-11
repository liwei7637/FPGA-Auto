"""Portable bootstrap: discover an existing project without changing it."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
import xml.etree.ElementTree as ET

from .config import load_config
from .jobs import atomic_json, job_state, read_json
from .results import FPGAError
from .vivado import _executable, verify_version
from .discovery import inventory, select_project, discover, SKIP_DIRS
from . import __version__


OPERATIONS = ("check", "synth", "quick", "impl", "build", "timing", "drc", "bit")


def find_projects(root):
    return [Path(item["project"]) for item in inventory(root)["candidates"] if item["exists"]]


def select_runs(project, synth_name=None, impl_name=None):
    try:
        document = ET.parse(str(project)).getroot()
    except (OSError, ET.ParseError) as exc:
        raise FPGAError("project_xml_invalid", str(exc)) from exc
    filesets = {item.get("Name"): item.get("Type") for item in document.findall("./FileSets/FileSet")}
    runs = document.findall("./Runs/Run")
    synths = {run.get("Id"): run for run in runs if run.get("Type", "").endswith(":Synth")
              and filesets.get(run.get("SrcSet")) == "DesignSrcs"}
    candidates = [run for run in runs if run.get("Type", "").endswith(":EntireDesign")
                  and run.get("SynthRun") in synths
                  and (synth_name is None or run.get("SynthRun") == synth_name)
                  and (impl_name is None or run.get("Id") == impl_name)]
    current = [run for run in candidates if run.get("State") == "current"]
    choices = current if len(current) == 1 else candidates
    if len(choices) != 1:
        pairs = [{"synth_run": run.get("SynthRun"), "impl_run": run.get("Id")} for run in candidates]
        raise FPGAError("run_selection_required", "Specify --synth-run and --impl-run; candidates: "
                        + json.dumps(pairs, ensure_ascii=False))
    return choices[0].get("SynthRun"), choices[0].get("Id")


def ensure_config(root, config_path, project=None, synth=None, impl=None, jobs=None):
    config_path = Path(config_path).resolve()
    selected, _ = select_project(root, config_path, project)
    if config_path.exists():
        config = load_config(config_path)
        requested = {"synth_run": synth, "impl_run": impl, "jobs": jobs}
        if project is not None:
            requested["project_path"] = selected
        if any(value is not None and config[key] != value for key, value in requested.items()):
            raise FPGAError("config_exists", "Existing configuration is preserved; choose a new --config path")
        return config, False
    if not selected.is_file() or selected.suffix.lower() != ".xpr":
        raise FPGAError("project_missing", "XPR not found: " + str(selected))
    synth_run, impl_run = select_runs(selected, synth, impl)
    try:
        relative = Path(os.path.relpath(str(selected), str(config_path.parent))).as_posix()
    except ValueError as exc:
        raise FPGAError("config_invalid", "Configuration and project must be on the same drive") from exc
    count = jobs if jobs is not None else min(8, os.cpu_count() or 1)
    if type(count) is not int or not 1 <= count <= 256:
        raise FPGAError("config_invalid", "jobs must be an integer from 1 to 256")
    data = dict(vivado_version="2020.2", project=relative, synth_run=synth_run, impl_run=impl_run, jobs=count)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(config_path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Windows rename is atomic and fails if the destination already exists.
        # It also works where NTFS hard-link creation is unavailable.
        try:
            if os.name == "nt":
                os.rename(str(temporary), str(config_path))
            else:
                os.link(str(temporary), str(config_path))
            created = True
        except FileExistsError:
            created = False
    finally:
        if temporary.exists():
            temporary.unlink()
    config = load_config(config_path)
    if config["project_path"] != selected or any(config[key] != data[key] for key in ("synth_run", "impl_run", "jobs")):
        raise FPGAError("config_exists", "Another initializer selected different settings; existing configuration preserved")
    return config, created


def vivado_candidates():
    values = [value for value in (os.environ.get("XILINX_VIVADO"), shutil.which("vivado")) if value]
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        for folder in ("Xilinx/Vivado/2020.2", "AMD/Vivado/2020.2", "vivado_2020_2/Vivado/2020.2",
                       "vivado2020.2/Vivado/2020.2"):
            values.append(drive + ":/" + folder)
    found = []
    for value in values:
        candidate = _executable(value)
        if candidate and candidate not in found:
            found.append(candidate)
    return found


def resolve_vivado(config_path, explicit=None, validate=False):
    cache = config_path.parent / ".fpga" / "portable" / "toolchain.json"
    override = explicit or os.environ.get("FPGA_VIVADO")
    if override:
        executable = _executable(override)
        if executable is None:
            raise FPGAError("vivado_not_found", "Vivado path does not exist: " + str(override))
        if validate:
            verify_version(executable)
            cache.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(cache, {"vivado_version": "2020.2", "executable": str(executable)})
        return executable
    try:
        saved = read_json(cache)
        executable = _executable(saved.get("executable", "")) if saved.get("vivado_version") == "2020.2" else None
        if executable:
            if validate:
                verify_version(executable)
            return executable
    except (OSError, ValueError, TypeError):
        pass
    failures = []
    for candidate in vivado_candidates():
        try:
            verify_version(candidate)
        except FPGAError as exc:
            failures.append(str(candidate) + ": " + exc.reason)
            continue
        cache.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(cache, {"vivado_version": "2020.2", "executable": str(candidate)})
        return candidate
    raise FPGAError("vivado_not_found", "Install Vivado 2020.2 or pass --vivado INSTALL_DIRECTORY. " + "; ".join(failures))


def make_parser():
    parser = argparse.ArgumentParser(description="Unpack-and-use Vivado 2020.2 CLI (Windows)")
    parser.add_argument("--version", action="version", version="FPGA Auto " + __version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("discover", "init", "start", "notify-check", "notify-retry", "notify-ack", "cancel") + OPERATIONS + ("status", "logs", "monitor", "menu"):
        command = commands.add_parser(name)
        command.add_argument("--root", help="Project root; default: parent of the tool directory")
        command.add_argument("--config", help="Default: <root>/fpga.json; relative paths use root")
        command.add_argument("--json", action="store_true", dest="json_output")
        if name in OPERATIONS or name in ("init", "start"):
            command.add_argument("--vivado", help="Vivado 2020.2 installation directory or executable")
        if name in OPERATIONS or name == "start":
            command.add_argument("--timeout", type=float, default=14400)
            command.add_argument("--no-monitor", action="store_true", help="Do not open the local progress/log dashboard")
        if name in OPERATIONS or name in ("init", "start", "discover"):
            command.add_argument("--project", help="Exact XPR path, manifest alias or unique filename/stem; paths use root")
        if name in OPERATIONS or name in ("init", "start"):
            command.add_argument("--synth-run")
            command.add_argument("--impl-run")
            command.add_argument("--jobs", type=int)
        if name == "discover":
            command.add_argument("--include-history", action="store_true", help="Include history in inventory, not automatic selection")
        if name == "start":
            command.add_argument("operation", choices=OPERATIONS)
        if name in ("build", "start"):
            command.add_argument("--bit", action="store_true", help="Generate a bitstream after build acceptance passes")
        if name in ("start", "notify-check"):
            command.add_argument("--notify-thread", help="Exact Codex desktop task UUID; notify-check defaults to current task")
        if name in ("start", "notify-check"):
            command.add_argument("--codex", help="Explicit legacy queue transport; default uses current desktop app-tools pipe")
        if name in ("cancel", "notify-retry", "notify-ack"):
            command.add_argument("--job", required=True)
        if name == "notify-ack":
            command.add_argument("--event", required=True)
        if name == "notify-retry":
            command.add_argument("--retry-uncertain", action="store_true", help="Allow possible duplicate after checking target task")
        if name in ("status", "logs"):
            command.add_argument("--job")
        if name == "monitor":
            command.add_argument("--job", required=True, help="Exact job ID to observe")
            command.add_argument("--no-open", action="store_true", help="Return dashboard URL without opening the browser")
        if name == "logs":
            command.add_argument("--follow", action="store_true")
    return parser


def menu(root, config_path):
    while True:
        print("\nFPGA Auto / Vivado 2020.2\n工程目录: " + str(root))
        print("1 初始化   2 检查   3 完整构建   4 状态   5 查看任务日志   6 进度与日志窗口   7 生成 bit   8 完整构建并生成 bit   0 退出")
        choice = input("选择: ").strip()
        if choice == "0":
            return 0
        command = {"1": "init", "2": "check", "3": "build", "4": "status", "5": "logs", "6": "monitor", "7": "bit", "8": "build"}.get(choice)
        if not command:
            continue
        arguments = [command, "--root", str(root), "--config", str(config_path)]
        if choice == "8":
            arguments.append("--bit")
        if command in ("logs", "monitor"):
            paths = sorted((config_path.parent / ".fpga" / "jobs").glob("*/state.json"), reverse=True)[:12]
            if not paths:
                print("暂无任务。")
                continue
            for index, path in enumerate(paths, 1):
                try:
                    state = job_state(path.parent)
                    print("{}: {}  {} / {}".format(index, path.parent.name, state["state"], state["stage"]))
                except (OSError, ValueError, KeyError):
                    print("{}: {} (记录无法读取)".format(index, path.parent.name))
            selected = input("日志任务编号，直接回车返回: ").strip()
            if not selected.isdigit() or not 1 <= int(selected) <= len(paths):
                continue
            arguments.extend(["--job", paths[int(selected) - 1].parent.name])
            if command == "logs":
                arguments.append("--follow")
                print("Ctrl+C 停止查看日志，不影响另一个窗口中的构建。")
        if command == "build":
            print("开始构建，自动打开进度与日志窗口。本终端 Ctrl+C 取消构建；关闭监控页面不影响构建。")
        code = main(arguments, default_root=root)
        print("退出码: " + str(code))


def main(argv=None, default_root=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = make_parser().parse_args(argv or ["menu"])
    if args.json_output and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    root = Path(args.root or default_root or Path.cwd()).resolve()
    config_path = (root / (args.config or "fpga.json")).resolve()
    try:
        if os.name != "nt":
            raise FPGAError("platform_unsupported", "Portable package requires Windows")
        if args.command == "menu":
            return menu(root, config_path)
        if args.command == "discover":
            print(json.dumps(discover(root, config_path, args.project, args.include_history), ensure_ascii=False, indent=2))
            return 0
        if args.command == "monitor":
            from .jobs import locate_job
            from .monitor import launch
            result = launch(locate_job(config_path, args.job), open_browser=not args.no_open)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "ready" else 1
        if args.command in ("start", "notify-check", "notify-retry", "notify-ack", "cancel"):
            from . import background
            if args.command == "notify-check":
                result = background.probe_notification(args.codex, args.notify_thread)
            elif args.command == "notify-ack":
                result = background.acknowledge(config_path, args.job, args.event)
            elif args.command == "cancel":
                result = background.cancel(config_path, args.job)
            elif args.command == "notify-retry":
                from .jobs import locate_job
                directory = locate_job(config_path, args.job)
                result = background.deliver(directory, args.retry_uncertain)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["status"] in ("queued", "disabled", "suppressed") else 1
            else:
                background.validate_timeout(args.timeout)
                if args.bit and args.operation != "build":
                    raise FPGAError("arguments_invalid", "--bit is only valid with start build; use start bit for existing results")
                config, _ = ensure_config(root, config_path, args.project, args.synth_run, args.impl_run, args.jobs)
                executable = resolve_vivado(config_path, args.vivado)
                result = background.start(root, config, args.operation, executable, args.timeout, args.notify_thread, args.codex,
                                          bitstream=args.bit)
                if not args.no_monitor and result.get("execution") == "submitted":
                    from .monitor import launch
                    result["monitor"] = launch(result["paths"]["job"])
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if result.get("execution") == "failed" else 0
        if args.command == "init":
            config, created = ensure_config(root, config_path, args.project, args.synth_run, args.impl_run, args.jobs)
            executable = resolve_vivado(config_path, args.vivado, validate=True)
            result = {"execution": "completed", "command": "init", "config": str(config_path),
                      "config_created": created, "project": str(config["project_path"]),
                      "synth_run": config["synth_run"], "impl_run": config["impl_run"],
                      "vivado_executable": str(executable), "vivado_version": "2020.2"}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        forwarded = [args.command, "--config", str(config_path)]
        if args.json_output:
            forwarded.append("--json")
        if args.command in OPERATIONS:
            if getattr(args, "bit", False):
                forwarded.append("--bit")
            ensure_config(root, config_path, args.project, args.synth_run, args.impl_run, args.jobs)
            executable = resolve_vivado(config_path, args.vivado)
            forwarded.extend(["--vivado", str(executable), "--timeout", str(args.timeout)])
            if args.no_monitor:
                forwarded.append("--no-monitor")
        else:
            if args.job:
                forwarded.extend(["--job", args.job])
            if args.command == "logs" and args.follow:
                forwarded.append("--follow")
        from .cli import main as cli_main
        return cli_main(forwarded)
    except KeyboardInterrupt:
        result = dict(execution="failed", stage="bootstrap", reason="cancelled", message="Interrupted by user")
        code = 130
    except (FPGAError, OSError, ValueError) as exc:
        result = dict(execution="failed", stage="bootstrap", reason=getattr(exc, "reason", "bootstrap_failed"), message=str(exc))
        if result["reason"] in ("project_selection_required", "project_missing", "config_exists"):
            try:
                result["discovery"] = discover(root, config_path, getattr(args, "project", None))
            except (FPGAError, OSError, ValueError):
                pass
        code = 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return code
