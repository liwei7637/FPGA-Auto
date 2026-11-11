"""Reject known constraint failures / path segmentation; silence never proves timing."""
from pathlib import Path
import re


TOP_RUNS = {
    "synth": ("synthesis",),
    "quick": ("synthesis",),
    "impl": ("synthesis", "implementation"),
    "build": ("synthesis", "implementation"),
    "timing": ("synthesis", "implementation"),
    "drc": ("synthesis", "implementation"),
    "bit": ("synthesis", "implementation"),
}
# 18-513 rejects invalid set_bus_skew startpoints/endpoints; 18-515 means
# set_max_delay segmented a path. Both invalidate intended CDC coverage.
DIAGNOSTIC = re.compile(
    r"^[ \t]*(?P<severity>CRITICAL WARNING|ERROR):[ \t]*"
    r"\[(?P<id>Designutils 20-1307|Common 17-1548|Constraints 18-513|Constraints 18-515)\][ \t]*(?P<message>.*)$")


def inspect_constraint_loading(command, reports, top_runs=None):
    """Read only this job's main log and required top-run log archives.

    Exact diagnostic IDs identify unsupported XDC commands, failed commands,
    invalid timing endpoints, or path segmentation that invalidates coverage.
    Other vendor warnings, including unrelated critical warnings, are evidence
    for review but are not automatically rejected by this narrowly scoped gate.
    """
    result = dict(status="not_applicable", reason=None, diagnostics=[], logs=[], missing_logs=[])
    if command not in TOP_RUNS:
        return result
    reports = Path(reports)
    paths = [reports.parent / "vivado.log"]
    paths.extend(reports / (stage + "_run.log") for stage in (TOP_RUNS[command] if top_runs is None else top_runs))
    for path in paths:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            if not content.strip():
                raise ValueError("empty log")
        except (OSError, ValueError):
            result["missing_logs"].append(str(path))
            result["logs"].append(dict(path=str(path), status="unavailable"))
            continue
        result["logs"].append(dict(path=str(path), status="available"))
        lines = content.splitlines()
        for index, line in enumerate(lines):
            match = DIAGNOSTIC.match(line)
            if match:
                entry = dict(match.groupdict(), log=str(path), line=index + 1)
                if index + 1 < len(lines) and re.match(r"^[ \t]*\[.*\.xdc:\d+\]", lines[index + 1], re.I):
                    entry["context"] = lines[index + 1].strip()
                result["diagnostics"].append(entry)
    if result["diagnostics"]:
        result.update(status="failed", reason="constraint_loading_failed")
    elif result["missing_logs"]:
        result.update(status="unknown", reason="constraint_loading_logs_unavailable")
    else:
        result.update(status="passed")
    return result


def failure_message(result):
    if result["diagnostics"]:
        first = result["diagnostics"][0]
        return "Known constraint-loading failure {} at {}:{}: {} ({} diagnostic occurrences)".format(
            first["id"], first["log"], first["line"], first["message"], len(result["diagnostics"]))
    return "Required constraint-loading log evidence is unavailable: " + "; ".join(result["missing_logs"])
