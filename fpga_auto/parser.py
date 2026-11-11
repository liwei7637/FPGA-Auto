"""Conservative parsers for Vivado 2020.2 reports, never log-based completion."""
import math
import re
from pathlib import Path


TIMING_COLUMNS = (
    "wns_ns", "tns_ns", "setup_failing_endpoints", "setup_total_endpoints",
    "whs_ns", "ths_ns", "hold_failing_endpoints", "hold_total_endpoints",
    "wpws_ns", "tpws_ns", "pulse_width_failing_endpoints", "pulse_width_total_endpoints",
)


def report_text(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_timing(text):
    result = dict.fromkeys(TIMING_COLUMNS)
    result.update(status="unknown", failing_endpoints=None, reason="timing_summary_missing")
    version = re.search(r"Tool Version\s*:\s*Vivado v\.?(\d{4}\.\d+)", text)
    if version and version.group(1) != "2020.2":
        result["reason"] = "timing_report_version_mismatch"
        return result
    if "Design Timing Summary" not in text:
        return result
    summary = text.split("Design Timing Summary", 1)[1].split("Clock Summary", 1)[0]
    lines = summary.splitlines()
    header = next((i for i, line in enumerate(lines) if re.match(r"\s*WNS\(ns\)\s+TNS\(ns\)", line)
                   and "THS Failing Endpoints" in line and "TPWS Total Endpoints" in line), None)
    if header is None:
        return result
    row = None
    for line in lines[header + 1:]:
        if not line.strip() or re.fullmatch(r"[\s-]+", line):
            continue
        row = line.split()
        break
    if row is None or len(row) != len(TIMING_COLUMNS):
        result["reason"] = "timing_summary_unparseable"
        return result
    for key, value in zip(TIMING_COLUMNS, row):
        try:
            if key.endswith("_ns"):
                parsed = float(value)
                result[key] = parsed if math.isfinite(parsed) else None
            else:
                result[key] = int(value) if re.fullmatch(r"\d+", value) else None
        except ValueError:
            pass
    # V1's legacy singular field is the setup (TNS) failing endpoint count.
    # Hold/pulse counts are separate; summing would double-count physical endpoints.
    result["failing_endpoints"] = result["setup_failing_endpoints"]
    if any(result[key] is None for key in TIMING_COLUMNS):
        result["reason"] = "timing_data_incomplete"
        return result
    for kind in ("setup", "hold", "pulse_width"):
        if result[kind + "_failing_endpoints"] > result[kind + "_total_endpoints"]:
            result["reason"] = "timing_counts_invalid"
            return result
    if any(result[key] > 0 for key in ("tns_ns", "ths_ns", "tpws_ns")):
        result["reason"] = "timing_totals_invalid"
        return result
    if any(result[key] < 0 for key in TIMING_COLUMNS if key.endswith("_ns")) or any(
            result[kind + "_failing_endpoints"] for kind in ("setup", "hold", "pulse_width")):
        result.update(status="failed", reason="timing_violations")
    elif not result["setup_total_endpoints"] or not result["hold_total_endpoints"]:
        result["reason"] = "timing_paths_missing"
    elif "All user specified timing constraints are met." not in summary:
        result["reason"] = "timing_completion_statement_missing"
    else:
        result.update(status="passed", reason=None)
    return result


def parse_drc(text):
    fields = {"error": "errors", "critical warning": "critical_warnings", "warning": "warnings",
              "advisory": "advisories", "info": "infos"}
    result = dict.fromkeys(fields.values())
    result.update(status="unknown", total_violations=None, reason="drc_summary_missing")
    if "Report DRC" not in text:
        return result
    version = re.search(r"Tool Version\s*:\s*Vivado v\.?(\d{4}\.\d+)", text)
    if version and version.group(1) != "2020.2":
        result["reason"] = "drc_report_version_mismatch"
        return result
    section = re.search(r"\n1\. REPORT SUMMARY\s*\n-+\s*\n(.*?)(?:\n2\. REPORT DETAILS|\Z)", text, re.S)
    if section is None:
        return result
    summary = section.group(1)
    total = re.search(r"\bViolations found:\s*(\d+)\b", summary, re.I)
    if total:
        total = int(total.group(1))
    elif re.search(r"\bNo DRC violations found\b", summary, re.I):
        total = 0
    else:
        return result
    counts = dict.fromkeys(fields.values(), 0)
    for match in re.finditer(r"^\s*\|\s*([^|]+)\|\s*([^|]+)\|([^|]*)\|\s*(\d+)\s*\|", summary, re.M):
        severity = " ".join(match.group(2).lower().split())
        if severity not in fields:
            result["reason"] = "drc_unknown_severity"
            return result
        counts[fields[severity]] += int(match.group(4))
    if sum(counts.values()) != total:
        result["reason"] = "drc_counts_incomplete"
        return result
    result.update(counts)
    failed = bool(counts["errors"] or counts["critical_warnings"])
    result.update(total_violations=total, status="failed" if failed else "passed",
                  reason="drc_violations" if failed else None)
    return result
