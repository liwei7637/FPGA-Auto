"""Discover and verify exactly Vivado 2020.2."""
import os
import re
import shutil
import tempfile
from pathlib import Path
from .results import FPGAError


def command_line(executable, args):
    values = [str(executable)] + [str(x) for x in args]
    if os.name == "nt" and Path(executable).suffix.lower() in (".bat", ".cmd"):
        # cmd.exe expands percent variables even within quotes. Reject such values
        # rather than permitting shell expansion of user-supplied paths.
        if any(any(c in v for c in '"%!\r\n\x00') for v in values):
            raise FPGAError("unsafe_batch_argument", "Batch arguments cannot contain quotes, %, ! or newlines")
        shell = str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
        return '"{}" /d /v:off /s /c "{}"'.format(shell, " ".join('"' + v + '"' for v in values))
    return values


def _executable(value):
    path = Path(value).expanduser()
    if path.is_dir():
        path = path / "bin" / "vivado.bat"
    return path.resolve() if path.is_file() else None


def find_vivado(explicit=None):
    if explicit:
        candidate = _executable(explicit)
        if candidate is None:
            raise FPGAError("vivado_not_found", "Explicit Vivado path does not exist: " + explicit)
        return candidate
    for name in ("FPGA_VIVADO", "XILINX_VIVADO"):
        if os.environ.get(name):
            candidate = _executable(os.environ[name])
            if candidate is None:
                raise FPGAError("vivado_not_found", name + " does not identify a Vivado installation")
            return candidate
    path = shutil.which("vivado")
    if path:
        return Path(path).resolve()
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        for folder in ("Xilinx/Vivado/2020.2", "AMD/Vivado/2020.2",
                       "vivado_2020_2/Vivado/2020.2", "vivado2020.2/Vivado/2020.2"):
            candidate = _executable(drive + ":/" + folder)
            if candidate:
                return candidate
    raise FPGAError("vivado_not_found", "Vivado 2020.2 not found; use --vivado or FPGA_VIVADO")


def parse_version(output):
    match = re.search(r"\bVivado\s+v?(\d{4}\.\d+)\b", output)
    return match.group(1) if match else None


def verify_version(executable, log_path=None, timeout=60):
    from .runner import run_process
    if log_path is None:
        with tempfile.TemporaryDirectory() as directory:
            return verify_version(executable, Path(directory) / "version.log", timeout)
    log_path = Path(log_path)
    try:
        code = run_process(command_line(executable, ["-version"]), log_path, log_path.parent, timeout)
    except FPGAError as exc:
        if exc.reason == "timeout":
            raise FPGAError("vivado_version_timeout", str(exc)) from exc
        raise
    output = log_path.read_text(encoding="utf-8", errors="replace")
    if code:
        raise FPGAError("vivado_version_failed", "Vivado -version returned " + str(code))
    version = parse_version(output)
    if version != "2020.2":
        raise FPGAError("vivado_version_mismatch", "Required 2020.2; found " + str(version))
    return version
