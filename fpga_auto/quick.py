"""Shared MIG-derived pre-route screen; never full signoff."""
import math
import re
from pathlib import Path
from .parser import report_text
from .constraint_loading import inspect_constraint_loading, failure_message

REQUIRED_FILES = (
    "post_synth_timing.rpt", "post_synth_paths.rpt", "post_synth_fanout.rpt",
    "post_place_timing.rpt", "post_place_paths.rpt", "post_place_fanout.rpt",
    "post_place_congestion.rpt", "post_place_check_timing.rpt",
    "post_place_cdc.rpt", "post_place.dcp",
)

def parse_quick_setup(text):
    """Max-only pre-route screen; never substitute this for full timing."""
    result = dict(status="unknown", scope="pre_route_setup_only", wns_ns=None,
                  tns_ns=None, failing_endpoints=None, total_endpoints=None,
                  reason="quick_timing_missing")
    if not re.search(r"Tool Version\s*:\s*Vivado v\.?2020\.2\b", text):
        return result
    if "Design Timing Summary" not in text:
        return result
    section = text.split("Design Timing Summary", 1)[1].split("Clock Summary", 1)[0]
    lines = section.splitlines()
    header = next((i for i, line in enumerate(lines) if re.match(r"\s*WNS\(ns\)\s+TNS\(ns\)", line)
                   and "TNS Failing Endpoints" in line), None)
    if header is None:
        return result
    row = next((line.split() for line in lines[header+1:]
                if line.strip() and not re.fullmatch(r"[\s-]+", line)), [])
    try:
        wns, tns = float(row[0]), float(row[1])
        fails, total = int(row[2]), int(row[3])
    except (IndexError, ValueError):
        return result
    if not all(math.isfinite(value) for value in (wns, tns)) or not 0 <= fails <= total or tns > 0:
        return result
    result.update(wns_ns=wns, tns_ns=tns, failing_endpoints=fails, total_endpoints=total)
    if wns < 0 or tns < 0 or fails:
        result.update(status="failed", reason="quick_setup_violations")
    elif total > 0:
        result.update(status="passed", reason=None)
    return result


def apply_quick_acceptance(result, reports):
    directory = reports / "quick_check"
    screening = parse_quick_setup(report_text(directory / "post_place_timing.rpt"))
    screening["post_synth"] = parse_quick_setup(report_text(directory / "post_synth_timing.rpt"))
    screening["missing_files"] = [name for name in REQUIRED_FILES
        if not (directory / name).is_file() or (directory / name).stat().st_size == 0]
    if screening["missing_files"] or screening["post_synth"]["status"] == "unknown":
        if screening["status"] != "failed":
            screening["status"] = "unknown"
        screening["reason"] = "quick_evidence_incomplete"
    result["quick_screening"] = screening
    native = (Path(__file__).resolve().parents[1] / "tcl/quick_common.tcl").is_file()
    constraints = inspect_constraint_loading("quick", reports,
        top_runs=("synthesis", "quick") if native else ("synthesis",))
    result["constraint_loading"] = constraints
    states = [result["synthesis"]["status"], screening["status"], constraints["status"]]
    result["acceptance_scope"] = "quick_pre_route"
    result["acceptance"] = ("failed" if "failed" in states else
        "passed" if all(s == "passed" for s in states) else "unknown")
    if constraints["status"] in ("failed", "unknown"):
        result.update(reason=constraints["reason"], message=failure_message(constraints))
    elif result["acceptance"] != "passed":
        result.update(reason=screening.get("reason") or "quick_screening_incomplete",
            message="Pre-route screening only. Inspect quick_check evidence; routing, Timing/DRC and board validation remain separate.")
