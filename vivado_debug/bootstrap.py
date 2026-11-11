"""Opt-in MCP dependency paths; the FPGA Auto CLI keeps its stdlib-only path."""
import os
from pathlib import Path
import sys

_dll_handles = []


def enable_vendor():
    directory = Path(__file__).resolve().parent.parent / "vendor"
    for path in (directory, directory / "win32", directory / "win32/lib", directory / "pythonwin"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    if os.name == "nt":
        for path in (directory / "pywin32_system32", directory / "win32"):
            if path.is_dir():
                _dll_handles.append(os.add_dll_directory(str(path)))
