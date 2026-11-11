"""Portable project configuration; installation paths are CLI/environment only."""
import json
from pathlib import Path, PureWindowsPath
from .results import FPGAError


def load_config(path):
    path = Path(path).resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise FPGAError("config_invalid", str(exc)) from exc
    expected = {"vivado_version", "project", "synth_run", "impl_run", "jobs"}
    if not isinstance(data, dict) or set(data) != expected:
        raise FPGAError("config_invalid", "Expected exactly: " + ", ".join(sorted(expected)))
    if data["vivado_version"] != "2020.2":
        raise FPGAError("config_invalid", "vivado_version must be 2020.2")
    project = data["project"]
    if (not isinstance(project, str) or not project or
            PureWindowsPath(project).drive or PureWindowsPath(project).root or
            Path(project).is_absolute() or "\x00" in project):
        raise FPGAError("config_invalid", "project must be a relative path without a drive")
    for key in ("synth_run", "impl_run"):
        value = data[key]
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\t\x00"):
            raise FPGAError("config_invalid", key + " must be a nonempty run name")
    if type(data["jobs"]) is not int or not 1 <= data["jobs"] <= 256:
        raise FPGAError("config_invalid", "jobs must be an integer from 1 to 256")
    project = (path.parent / project.replace("\\", "/")).resolve()
    if project.suffix.lower() != ".xpr" or not project.is_file():
        raise FPGAError("project_missing", "XPR not found: " + str(project))
    data["project_path"] = project
    data["config_path"] = path
    return data
