"""Stable failure and result contracts."""


class FPGAError(Exception):
    def __init__(self, reason, message, stage="preflight"):
        super().__init__(message)
        self.reason = reason
        self.stage = stage


def new_result(command, job_id=None):
    return {
        "schema_version": 1, "job_id": job_id, "command": command,
        "execution": "failed", "acceptance": "unknown", "stage": "preflight",
        "acceptance_scope": "quick_pre_route" if command == "quick" else (
            command if command in ("build", "timing", "drc", "bit") else "none"),
        "reason": None, "message": None,
        "synthesis": {"status": "unknown"},
        "implementation": {"status": "unknown"},
        "bitstream": {"status": "not_requested"},
        "timing": {"status": "unknown", "wns_ns": None, "tns_ns": None,
                   "whs_ns": None, "ths_ns": None, "failing_endpoints": None},
        "drc": {"status": "unknown", "errors": None,
                "critical_warnings": None, "warnings": None},
        "paths": {},
    }


def exit_code(result):
    if result.get("reason") == "cancelled":
        return 130
    if result["execution"] != "completed":
        return 1
    if result["acceptance"] == "failed":
        return 2
    if result["acceptance_scope"] != "none" and result["acceptance"] == "unknown":
        return 3
    return 0


def merge_run_status(result, bridge):
    for stage in ("synthesis", "implementation"):
        status = bridge.get(stage + "_status", "unknown")
        if status in ("passed", "failed"):
            result[stage]["status"] = status
        if bridge.get(stage + "_run_status"):
            result[stage]["run_status"] = bridge[stage + "_run_status"]


def apply_acceptance(result, reports):
    from .parser import parse_timing, parse_drc, report_text
    command = result["command"]
    if command == "quick":
        from .quick import apply_quick_acceptance
        apply_quick_acceptance(result, reports)
        return
    if command in ("timing", "build", "bit"):
        result["timing"] = parse_timing(report_text(reports / "timing_summary.rpt"))
    if command in ("drc", "build", "bit"):
        result["drc"] = parse_drc(report_text(reports / "drc.rpt"))
    required = ("synthesis", "implementation", "timing", "drc") if command in ("build", "bit") else (
        (command,) if command in ("timing", "drc") else ())
    if not required:
        return
    statuses = [result[key]["status"] for key in required]
    result["acceptance"] = "failed" if "failed" in statuses else (
        "passed" if all(status == "passed" for status in statuses) else "unknown")
    if result["acceptance"] == "unknown":
        result.update(reason="acceptance_data_unavailable", message="; ".join(
            key + ": " + str(result[key].get("reason") or "phase status missing")
            for key in required if result[key]["status"] == "unknown"))
