"""Read-only project inventory and deterministic selection, without Vivado."""
import os
import stat
from pathlib import Path
import xml.etree.ElementTree as ET

from .config import load_config
from .jobs import read_json
from .results import FPGAError


SKIP_DIRS = {".git", ".fpga", ".agents", "fpga-auto", "dist", "__pycache__", ".venv", "node_modules"}
HISTORY_DIRS = {"build", "archive", "archives", "backup", "backups"}
GENERATED_SUFFIXES = (".runs", ".cache", ".gen", ".hw", ".sim", ".ip_user_files")


def reparse_directory(path):
    """Avoid traversing Windows junctions on Python versions before 3.12 too."""
    if os.name != "nt":
        return False
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return True


def historical(root, path):
    try:
        return any(part.lower() in HISTORY_DIRS for part in path.relative_to(root).parts[:-1])
    except ValueError:
        return False


def inventory(root, include_history=False):
    root = Path(root).resolve()
    records, warnings = {}, []
    manifest_path = root / "config" / "native-projects.json"
    manifest = {}

    def add(path, source, alias=None):
        path = Path(path).resolve()
        if historical(root, path) and not include_history:
            if alias:
                warnings.append({"reason": "historical_manifest_entry", "alias": alias, "project": str(path)})
            return
        key = os.path.normcase(str(path))
        record = records.setdefault(key, {"project": str(path), "name": path.stem,
                                         "aliases": [], "sources": [], "historical": historical(root, path)})
        if source not in record["sources"]:
            record["sources"].append(source)
        if alias and alias not in record["aliases"]:
            record["aliases"].append(alias)

    if manifest_path.is_file():
        try:
            manifest = read_json(manifest_path)
            entries = manifest.get("projects")
            if not isinstance(entries, dict):
                raise ValueError("projects must be an object keyed by project alias")
            if "default_project" in manifest and not isinstance(manifest["default_project"], str):
                raise ValueError("default_project must be a project alias or path")
            for alias, entry in entries.items():
                if not isinstance(entry, dict) or not isinstance(entry.get("project"), str) or not entry["project"]:
                    raise ValueError("Each project entry must contain a nonempty project path: " + alias)
                add(root / entry["project"], str(manifest_path), alias)
        except (OSError, ValueError) as exc:
            raise FPGAError("project_manifest_invalid", str(exc)) from exc

    for directory, dirs, files in os.walk(str(root), followlinks=False):
        dirs[:] = sorted(name for name in dirs if name.lower() not in SKIP_DIRS
                         and (include_history or name.lower() not in HISTORY_DIRS)
                         and not name.lower().endswith(GENERATED_SUFFIXES)
                         and not Path(directory, name).is_symlink()
                         and not reparse_directory(Path(directory, name)))
        for name in sorted(files):
            path = Path(directory, name)
            if path.suffix.lower() == ".xpr" and not path.is_symlink():
                add(path, "filesystem")

    candidates = sorted(records.values(), key=lambda item: item["project"].lower())
    for record in candidates:
        path = Path(record["project"])
        record["exists"] = path.is_file() and path.suffix.lower() == ".xpr"
        try:
            record["relative_path"] = path.relative_to(root).as_posix()
        except ValueError:
            record["relative_path"] = None
        if not record["exists"]:
            record["inspection_error"] = "project_missing"
            continue
        try:
            document = ET.parse(path).getroot()
            part = document.find("./Configuration/Option[@Name='Part']")
            record["part"] = part.get("Val") if part is not None else None
            filesets = {item.get("Name") for item in document.findall("./FileSets/FileSet")
                        if item.get("Type") == "DesignSrcs"}
            synths = {run.get("Id") for run in document.findall("./Runs/Run")
                      if run.get("Type", "").endswith(":Synth") and run.get("SrcSet") in filesets}
            record["runs"] = [{key: run.get(key) for key in ("Id", "Type", "SrcSet", "SynthRun", "State")}
                              for run in document.findall("./Runs/Run")
                              if run.get("Id") in synths or run.get("SynthRun") in synths]
            from .portable import select_runs
            record["synth_run"], record["impl_run"] = select_runs(path)
        except FPGAError as exc:
            record["inspection_error"] = exc.reason
        except (OSError, ET.ParseError) as exc:
            record["inspection_error"] = "project_xml_invalid"
            record["inspection_message"] = str(exc)
    return {"root": str(root), "manifest": str(manifest_path) if manifest else None,
            "default_project": manifest.get("default_project"), "candidates": candidates,
            "warnings": warnings, "excluded_directories": sorted(SKIP_DIRS | (set() if include_history else HISTORY_DIRS))}


def match_project(root, selector, candidates):
    # Paths are authoritative. A miss must never silently select a different design.
    path = (Path(root) / selector).resolve()
    if path.is_file() or Path(selector).is_absolute() or any(char in selector for char in "/\\:"):
        if not path.is_file() or path.suffix.lower() != ".xpr":
            raise FPGAError("project_missing", "XPR not found: " + str(path))
        return path
    token = selector.casefold()
    matches = [item for item in candidates if token in {
        item["name"].casefold(), Path(item["project"]).name.casefold(),
        *(name.casefold() for name in item["aliases"])}]
    if len(matches) != 1:
        raise FPGAError("project_selection_required" if matches else "project_missing",
                        "Use an exact XPR path or an unambiguous project alias/name: " + selector)
    if not matches[0]["exists"]:
        raise FPGAError("project_missing", "Manifest XPR is missing: " + matches[0]["project"])
    return Path(matches[0]["project"])


def select_project(root, config_path, selector=None, report=None):
    root = Path(root).resolve()
    # A directly supplied path works even when unrelated discovery metadata is broken.
    if selector is not None:
        path = (root / selector).resolve()
        if path.is_file() or Path(selector).is_absolute() or any(c in selector for c in "/\\:"):
            return match_project(root, selector, []), "explicit_path"
        report = report or inventory(root)
        return match_project(root, selector, report["candidates"]), "explicit_name"
    if Path(config_path).exists():
        return load_config(config_path)["project_path"], "saved_config"
    report = report or inventory(root)
    if report["default_project"]:
        return match_project(root, report["default_project"], report["candidates"]), "manifest_default"
    candidates = [item for item in report["candidates"] if not item["historical"]]
    if len(candidates) != 1:
        raise FPGAError("project_selection_required" if candidates else "project_missing",
                        "No unique active XPR. Run discover --json and select --project PATH or NAME.")
    if not candidates[0]["exists"]:
        raise FPGAError("project_missing", "Manifest XPR is missing: " + candidates[0]["project"])
    return Path(candidates[0]["project"]), "unique_candidate"


def discover(root, config_path, selector=None, include_history=False):
    report = inventory(root, include_history)
    report.update(command="discover", execution="completed")
    try:
        project, reason = select_project(root, config_path, selector, report)
        report["selection"] = {"status": "selected", "project": str(project), "reason": reason}
        if Path(config_path).exists() and selector is not None:
            saved = load_config(config_path)["project_path"]
            if saved != project:
                report["warnings"].append({"reason": "config_conflict", "configured_project": str(saved),
                                           "message": "Use a different --config path; existing configuration is preserved."})
    except FPGAError as exc:
        report["selection"] = {"status": "unresolved", "reason": exc.reason, "message": str(exc)}
    return report
