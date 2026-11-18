"""Build one daily dataset and generate HTML, Excel and JSON reports."""

import html
import json
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from celery_app import app
from reporting import reporting_store as rs

IST = ZoneInfo("Asia/Kolkata")
UTC = timezone.utc
AGENT_TYPES = (
    "api_monitor",
    "infrastructure_agent",
    "log_agent",
    "langfuse_agent",
)


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _extract_findings(agent_type, result):
    findings = []

    if not isinstance(result, dict):
        return findings

    if agent_type == "api_monitor":
        if not result.get("ok", False):
            findings.append({
                "agent": agent_type,
                "severity": "error",
                "message": result.get("error") or "API health check failed",
            })

    elif agent_type == "infrastructure_agent":
        if result.get("health") == "warning":
            findings.append({
                "agent": agent_type,
                "severity": "warning",
                "message": (
                    f"Infrastructure report warning; source age: "
                    f"{result.get('report_age_minutes', 'unknown')} minutes"
                ),
            })

        for container in result.get("containers", []):
            if container.get("state") != "running":
                findings.append({
                    "agent": agent_type,
                    "severity": "error",
                    "message": (
                        f"Container {container.get('name', 'unknown')} "
                        f"state: {container.get('state', 'unknown')}"
                    ),
                })

    elif agent_type in ("log_agent", "langfuse_agent"):
        for item in result.get("findings", []):
            findings.append({
                "agent": agent_type,
                "severity": item.get("severity", "info"),
                "service": item.get("service"),
                "message": item.get("message") or item.get("name")
                    or json.dumps(item, default=str),
                "historical": item.get("historical", False),
            })

        if result.get("health") == "warning" and not result.get("findings"):
            findings.append({
                "agent": agent_type,
                "severity": "warning",
                "message": "Agent reported warning status.",
            })

    return findings


def build_daily_dataset(report_date=None, cutoff_hour=20):
    """Aggregate all reporting cycles from midnight to 8 PM IST."""
    report_date = report_date or datetime.now(IST).date()

    start_ist = datetime.combine(report_date, time.min, tzinfo=IST)
    if not 1 <= cutoff_hour <= 24:
        raise ValueError("cutoff_hour must be between 1 and 24")

    end_ist = (
        start_ist + timedelta(days=1)
        if cutoff_hour == 24
        else datetime.combine(
            report_date, time(hour=cutoff_hour), tzinfo=IST
        )
    )

    cycles = rs.list_cycles_for_window(
        start_ist.astimezone(UTC),
        end_ist.astimezone(UTC),
    )

    by_window = {}
    for cycle in cycles:
        window_start = cycle["window_start"]
        if window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=UTC)
        key = window_start.astimezone(IST).isoformat()
        by_window[key] = cycle

    expected_starts = [
        start_ist + timedelta(minutes=15 * index)
        for index in range(int((end_ist - start_ist).total_seconds() / 900))
    ]

    missing_windows = [
        value.isoformat()
        for value in expected_starts
        if value.isoformat() not in by_window
    ]

    agent_summary = {
        name: {"completed": 0, "failed": 0, "missing": 0}
        for name in AGENT_TYPES
    }

    cycle_items = []
    findings = []

    for cycle in cycles:
        cycle_agents = {}
        for agent in cycle.get("agents", []):
            agent_type = agent["agent_type"]
            result = agent.get("result")
            status = agent.get("status", "missing")

            cycle_agents[agent_type] = {
                "status": status,
                "task_id": agent.get("task_id"),
                "result": result,
            }

            if agent_type in agent_summary:
                if status == "completed" and isinstance(result, dict):
                    agent_summary[agent_type]["completed"] += 1
                elif status in ("missing", "pending", "queued"):
                    agent_summary[agent_type]["missing"] += 1
                else:
                    agent_summary[agent_type]["failed"] += 1

            findings.extend(_extract_findings(agent_type, result))

        validation = cycle.get("validation") or {}
        if cycle.get("status") != "completed":
            findings.append({
                "agent": "reporting_orchestrator",
                "severity": "warning",
                "message": f"Cycle {cycle['id']} status: {cycle.get('status')}",
            })

        if validation.get("cycle_mismatches"):
            findings.append({
                "agent": "reporting_orchestrator",
                "severity": "error",
                "message": "Reporting cycle metadata mismatch detected.",
            })

        cycle_items.append({
            "cycle_id": cycle["id"],
            "cycle_key": cycle["cycle_key"],
            "window_start": _iso(cycle["window_start"]),
            "window_end": _iso(cycle["window_end"]),
            "status": cycle["status"],
            "validation": validation,
            "agents": cycle_agents,
        })

    for missing in missing_windows:
        findings.append({
            "agent": "reporting_scheduler",
            "severity": "warning",
            "message": f"No reporting cycle found for window starting {missing}",
        })

    completed_cycles = sum(
        1 for cycle in cycles if cycle.get("status") == "completed"
    )

    has_cycle_mismatches = any(
        bool((cycle.get("validation") or {}).get("cycle_mismatches"))
        for cycle in cycles
    )
    all_required_agents_completed = bool(expected_starts) and all(
        agent_summary[name]["completed"] == len(expected_starts)
        and agent_summary[name]["failed"] == 0
        and agent_summary[name]["missing"] == 0
        for name in AGENT_TYPES
    )

    validation = {
        "expected_cycles": len(expected_starts),
        "observed_cycles": len(cycles),
        "missing_cycles": len(missing_windows),
        "completed_cycles": completed_cycles,
        "all_expected_cycles_present": not missing_windows,
        "all_observed_cycles_completed": bool(cycles) and completed_cycles == len(cycles),
        "all_required_agents_completed": all_required_agents_completed,
        "has_cycle_mismatches": has_cycle_mismatches,
        "agent_summary": agent_summary,
    }

    return {
        "schema_version": "1.0",
        "report_date": report_date.isoformat(),
        "timezone": "Asia/Kolkata",
        "coverage_start": start_ist.isoformat(),
        "coverage_end": end_ist.isoformat(),
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "validation": validation,
        "cycles": cycle_items,
        "findings": findings,
    }


def _write_json(dataset, path):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dataset, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_html(dataset, path):
    rows = []
    for cycle in dataset["cycles"]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(cycle['window_start'])}</td>"
            f"<td>{html.escape(cycle['status'])}</td>"
            f"<td>{len(cycle['agents'])}</td>"
            "</tr>"
        )

    finding_rows = [
        "<tr>"
        f"<td>{html.escape(str(item.get('severity', 'info')))}</td>"
        f"<td>{html.escape(str(item.get('agent', '')))}</td>"
        f"<td>{html.escape(str(item.get('message', '')))}</td>"
        "</tr>"
        for item in dataset["findings"]
    ]

    validation = dataset["validation"]
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Daily Monitoring Report - {html.escape(dataset['report_date'])}</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 32px; color: #222; }}
table {{ border-collapse: collapse; width: 100%; margin: 16px 0 28px; }}
th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; }}
th {{ background: #eee; }}
</style>
</head>
<body>
<h1>Advanced RAG Agentic AI - Daily Monitoring Report</h1>
<p>Date: {html.escape(dataset['report_date'])} (IST)</p>
<p>Coverage: {html.escape(dataset['coverage_start'])} to
{html.escape(dataset['coverage_end'])}</p>
<h2>Summary</h2>
<ul>
<li>Expected cycles: {validation['expected_cycles']}</li>
<li>Observed cycles: {validation['observed_cycles']}</li>
<li>Completed cycles: {validation['completed_cycles']}</li>
<li>Missing cycles: {validation['missing_cycles']}</li>
<li>Findings: {len(dataset['findings'])}</li>
</ul>
<h2>Reporting Cycles</h2>
<table><tr><th>Window start</th><th>Status</th><th>Agent records</th></tr>
{''.join(rows)}</table>
<h2>Findings and Warnings</h2>
<table><tr><th>Severity</th><th>Source</th><th>Finding</th></tr>
{''.join(finding_rows)}</table>
</body></html>"""

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(path)



def _write_excel(dataset, path):
    """Generate the approved three-sheet, evidence-based Excel report."""
    from openpyxl.styles import Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    summary = workbook.active
    summary.title = "Manager Summary"
    detail = workbook.create_sheet("Detailed Report")
    readme = workbook.create_sheet("Read Me")

    navy = "203864"
    blue = "D9EAF7"
    green = "E2F0D9"
    red = "FCE4D6"
    grey = "E7E6E6"
    white = "FFFFFF"
    thin = Side(style="thin", color="D9E1F2")

    validation = dataset.get("validation", {})
    cycles = dataset.get("cycles", [])
    agent_summary = validation.get("agent_summary", {})
    findings = dataset.get("findings", [])

    def safe(value):
        if value is None:
            return "Not reported"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value)

    def status_for(value):
        value = str(value or "").lower()
        if value in ("healthy", "completed", "running", "pass", "ok"):
            return "PASS"
        if value in ("critical", "failed", "error", "unhealthy"):
            return "REVIEW"
        if value in ("warning", "partial", "dispatch_failed"):
            return "REVIEW"
        if value in ("missing", "pending", "queued", "cancelled"):
            return "MISSING"
        return "INFO"

    def add_detail(section, metric, value, status="INFO", action=""):
        detail.append([
            section, metric, safe(value), status, safe(action)
        ])


    # ---------------- Manager Summary: approved reference dashboard ----------------
    def latest_agent_result(agent_type):
        for cycle in reversed(cycles):
            agent = (cycle.get("agents") or {}).get(agent_type) or {}
            result = agent.get("result")
            if agent.get("status") == "completed" and isinstance(result, dict):
                return result
        return None

    api_result = latest_agent_result("api_monitor")
    infra_result = latest_agent_result("infrastructure_agent")
    log_result = latest_agent_result("log_agent")
    langfuse_result = latest_agent_result("langfuse_agent")

    containers = (infra_result or {}).get("containers") or []
    running_count = sum(
        1 for item in containers
        if str(item.get("state", "")).lower() == "running"
        and str(item.get("health", "")).lower() != "unhealthy"
    )
    container_count = len(containers)

    postgres_summaries = [
        item for item in ((infra_result or {}).get("log_summary") or [])
        if "postgres" in str(item.get("service", "")).lower()
    ]
    postgres_available = bool(postgres_summaries)
    postgres_count = sum(
        int(item.get("error_like_line_count") or 0)
        for item in postgres_summaries
    )
    if not postgres_available and log_result:
        postgres_evidence = [
            item for item in (log_result.get("evidence") or [])
            if "postgres" in str(item.get("service", "")).lower()
        ]
        if postgres_evidence:
            postgres_available = True
            postgres_count = sum(
                1 for item in postgres_evidence
                if str(item.get("severity", "")).lower() in ("error", "critical")
            )

    observations = (langfuse_result or {}).get("observations") or []
    severity_counts = (langfuse_result or {}).get("severity_counts") or {}
    langfuse_total = (
        langfuse_result.get("observations_analyzed")
        if langfuse_result else None
    )
    if langfuse_total is None and langfuse_result:
        langfuse_total = langfuse_result.get("observations_requested")
    langfuse_errors = (
        int(severity_counts.get("error", 0) or 0)
        + int(severity_counts.get("critical", 0) or 0)
    )
    if not severity_counts and observations:
        langfuse_errors = sum(
            1 for item in observations
            if str(item.get("severity", "")).lower() in ("error", "critical")
        )
    langfuse_available = langfuse_result is not None and langfuse_total is not None

    def add_panel(header_range, value_range, header, value, status):
        summary.merge_cells(header_range)
        summary.merge_cells(value_range)
        header_cell = summary[header_range.split(":")[0]]
        value_cell = summary[value_range.split(":")[0]]
        header_cell.value = header
        value_cell.value = value
        header_cell.fill = PatternFill("solid", fgColor="203864")
        header_cell.font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
        header_cell.alignment = Alignment(horizontal="center", vertical="center")
        fill, color = {
            "PASS": ("E2F0D9", "008000"),
            "REVIEW": ("FCE4D6", "C00000"),
            "MISSING": ("E7E6E6", "666666"),
        }.get(status, ("E7E6E6", "666666"))
        value_cell.fill = PatternFill("solid", fgColor=fill)
        value_cell.font = Font(name="Arial", size=14, bold=True, color=color)
        value_cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )

    summary.merge_cells("A1:D1")
    summary["A1"] = "ADVANCED RAG AGENTIC AI"
    summary.merge_cells("A2:D2")
    summary["A2"] = "Daily Infrastructure & AI Observability Report"
    summary.merge_cells("A3:B3")
    summary["A3"] = f"Reporting snapshot: {dataset.get('generated_at_utc') or 'Not reported'}"
    summary.merge_cells("C3:D3")
    summary["C3"] = f"Report generated: {dataset.get('report_date') or 'Not reported'}"

    if api_result is None:
        api_text, api_status = "NOT REPORTED — REVIEW", "MISSING"
    elif api_result.get("ok") is True:
        api_text = f"HTTP {api_result.get('http_status', 200)} — PASSED"
        api_status = "PASS"
    else:
        api_text = f"HTTP {api_result.get('http_status', 'FAILED')} — REVIEW"
        api_status = "REVIEW"

    if not containers:
        container_text, container_status = "NOT REPORTED — REVIEW", "MISSING"
    else:
        container_text = (
            f"{running_count} OF {container_count} LISTED — "
            f"{'RUNNING' if running_count == container_count else 'REVIEW'}"
        )
        container_status = "PASS" if running_count == container_count else "REVIEW"

    if not postgres_available:
        postgres_text, postgres_status = "NOT REPORTED — REVIEW", "MISSING"
    elif postgres_count:
        postgres_text, postgres_status = f"{postgres_count} — REVIEW REQUIRED", "REVIEW"
    else:
        postgres_text, postgres_status = "0 — NO ERROR-LIKE MATCHES", "PASS"

    if not langfuse_available:
        langfuse_text, langfuse_status = "NOT REPORTED — REVIEW", "MISSING"
    else:
        langfuse_text = f"{langfuse_total} TOTAL · {langfuse_errors} ERROR-LIKE"
        langfuse_status = "REVIEW" if langfuse_errors else "PASS"

    add_panel("A5:B5", "A6:B6", "API HEALTH", api_text, api_status)
    add_panel("C5:D5", "C6:D6", "APPLICATION CONTAINERS", container_text, container_status)
    add_panel("A8:B8", "A9:B9", "POSTGRESQL LOG MATCHES", postgres_text, postgres_status)
    add_panel("C8:D8", "C9:D9", "LANGFUSE OBSERVATIONS", langfuse_text, langfuse_status)

    for row, height in {
        1: 42, 2: 30, 3: 34, 4: 18, 5: 26, 6: 46,
        7: 18, 8: 26, 9: 46, 10: 18, 11: 30, 12: 28,
        13: 18, 14: 24, 20: 30, 21: 12, 22: 42,
    }.items():
        summary.row_dimensions[row].height = height

    missing = int(validation.get("missing_cycles", 0) or 0)
    expected = int(validation.get("expected_cycles", 0) or 0)
    observed = int(validation.get("observed_cycles", 0) or 0)
    coverage_status = "PASS" if expected > 0 and missing == 0 else "REVIEW"

    assessment = (
        f"API health: {api_text.lower()}. "
        f"Application containers: {container_text.lower()}. "
        f"PostgreSQL: {postgres_text.lower()}. "
        f"Langfuse: {langfuse_text.lower()}. "
    )
    if missing:
        assessment += (
            f"Reporting coverage is incomplete: {observed} of {expected} "
            f"expected cycles observed; {missing} windows are missing. "
        )
    assessment += (
        "Log matches and error-like observations are investigation indicators, "
        "not confirmation of an active incident."
    )
    summary.merge_cells("A11:D12")
    summary["A11"] = "Executive assessment: " + assessment
    summary["A11"].fill = PatternFill("solid", fgColor="F2F2F2")
    summary["A11"].font = Font(name="Arial", size=11, color="000000")
    summary["A11"].alignment = Alignment(wrap_text=True, vertical="center")

    summary.merge_cells("B14:D14")
    summary["A14"] = "Priority"
    summary["B14"] = "Recommended action"
    for cell in summary[14]:
        cell.fill = PatternFill("solid", fgColor="203864")
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center")

    priorities = []
    if missing:
        priorities.append(("HIGH", f"Investigate {missing} missing 15-minute reporting windows; verify scheduler, Beat and worker execution."))
    if api_status != "PASS":
        priorities.append(("HIGH", "Restore API health monitoring and verify the configured health endpoint."))
    if container_status != "PASS":
        priorities.append(("HIGH", "Verify application container inventory, state and health checks."))
    if postgres_status != "PASS":
        priorities.append(("HIGH", "Review PostgreSQL log evidence, timestamps, severity and surrounding context."))
    if langfuse_status != "PASS":
        priorities.append(("HIGH", "Inspect Langfuse observations, trace status, exceptions and related spans."))
    if not priorities:
        priorities.append(("INFO", "No priority actions were identified from the available monitoring evidence."))

    for row_num, (priority, action) in enumerate(priorities[:4], start=15):
        summary.cell(row_num, 1, priority)
        summary.merge_cells(start_row=row_num, start_column=2, end_row=row_num, end_column=4)
        summary.cell(row_num, 2, action)
        summary.cell(row_num, 1).font = Font(
            name="Arial", bold=True,
            color="C00000" if priority == "HIGH" else "BF9000"
        )
        summary.cell(row_num, 1).fill = PatternFill(
            "solid", fgColor="FCE4D6" if priority == "HIGH" else "FFF2CC"
        )
        summary.cell(row_num, 1).alignment = Alignment(horizontal="center", vertical="center")
        summary.cell(row_num, 2).alignment = Alignment(wrap_text=True, vertical="center")
        summary.row_dimensions[row_num].height = 34

    summary.merge_cells("A20:D20")
    summary["A20"] = (
        f"REPORTING COMPLETENESS: {observed}/{expected} cycles observed · "
        f"{missing} missing · {coverage_status}"
    )
    summary["A20"].fill = PatternFill(
        "solid", fgColor="E2F0D9" if coverage_status == "PASS" else "FCE4D6"
    )
    summary["A20"].font = Font(
        name="Arial", size=11, bold=True,
        color="008000" if coverage_status == "PASS" else "C00000"
    )
    summary["A20"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    summary.merge_cells("A22:D22")
    summary["A22"] = (
        "Data limitation: Dashboard values reflect monitoring evidence available "
        "at report generation time. Missing cycles or unavailable agent results "
        "are not treated as healthy. Log matches and observation indicators "
        "require review of source evidence before confirming an incident."
    )
    summary["A22"].fill = PatternFill("solid", fgColor="F2F2F2")
    summary["A22"].font = Font(name="Arial", size=9, italic=True, color="404040")
    summary["A22"].alignment = Alignment(wrap_text=True, vertical="center")

    # ---------------- Detailed Report ----------------
    detail.merge_cells("A1:E1")
    detail["A1"] = "ADVANCED RAG AGENTIC AI — DETAILED DAILY REPORT"
    detail.merge_cells("A2:E2")
    detail["A2"] = (
        f"Report date: {dataset.get('report_date')} | "
        f"Coverage: {dataset.get('coverage_start')} to "
        f"{dataset.get('coverage_end')} | "
        f"Timezone: {dataset.get('timezone')}"
    )
    detail.append([])
    detail.append([
        "Section", "Metric", "Reported Value", "Status",
        "Details / Recommended Action"
    ])

    add_detail(
        "1. EXECUTIVE SUMMARY", "Observed reporting cycles",
        validation.get("observed_cycles", 0), "INFO",
        "Number of cycles retrieved for this report date."
    )
    add_detail(
        "1. EXECUTIVE SUMMARY", "Expected reporting cycles",
        validation.get("expected_cycles", 0), "INFO",
        "Expected 15-minute windows for the configured coverage period."
    )
    add_detail(
        "1. EXECUTIVE SUMMARY", "Missing reporting cycles",
        validation.get("missing_cycles", 0),
        "REVIEW" if validation.get("missing_cycles", 0) else "PASS",
        "Investigate missing windows; no agent result is assumed for a missing cycle."
    )
    add_detail(
        "1. EXECUTIVE SUMMARY", "Completed reporting cycles",
        validation.get("completed_cycles", 0), "INFO",
        "Cycles whose orchestrator status is completed."
    )

    for cycle in cycles:
        cycle_label = f"Cycle {cycle.get('cycle_key') or cycle.get('cycle_id')}"
        cycle_status = status_for(cycle.get("status"))
        add_detail(
            "7. REPORTING CYCLE VALIDATION",
            f"{cycle_label} — cycle status",
            cycle.get("status"), cycle_status,
            f"Cycle ID: {cycle.get('cycle_id')}; "
            f"window: {cycle.get('window_start')} to {cycle.get('window_end')}."
        )
        cycle_validation = cycle.get("validation") or {}
        add_detail(
            "7. REPORTING CYCLE VALIDATION",
            f"{cycle_label} — validation",
            cycle_validation, 
            "PASS" if cycle_validation.get("same_reporting_cycle") else "REVIEW",
            "Checks that all agent results belong to the same reporting window."
        )

        agents = cycle.get("agents") or {}
        for agent_type in AGENT_TYPES:
            agent = agents.get(agent_type) or {}
            result = agent.get("result")
            agent_status = agent.get("status", "missing")
            section = f"8. AGENT EXECUTION — {agent_type.upper()}"
            add_detail(
                section, "Execution status", agent_status,
                status_for(agent_status),
                f"Task ID: {agent.get('task_id') or 'Not available'}"
            )
            add_detail(
                section, "Task ID", agent.get("task_id"), "INFO",
                "Celery task identifier associated with this agent execution."
            )
            if not isinstance(result, dict):
                add_detail(
                    section, "Agent result", "No result payload",
                    "MISSING",
                    "Check task error summary and worker logs."
                )
                continue

            if agent_type == "api_monitor":
                add_detail(
                    "4. API MONITORING", "API URL", result.get("url"), "INFO",
                    "Endpoint configured for the API health probe."
                )
                add_detail(
                    "4. API MONITORING", "HTTP status",
                    result.get("http_status"),
                    "PASS" if result.get("ok") else "REVIEW",
                    result.get("error") or "HTTP health probe result."
                )
                add_detail(
                    "4. API MONITORING", "Response time (ms)",
                    result.get("response_time_ms"), "INFO",
                    "Measured duration of this API health request."
                )
                add_detail(
                    "4. API MONITORING", "Response excerpt",
                    result.get("response_excerpt"), "INFO",
                    "Bounded response body excerpt returned by the health endpoint."
                )
                add_detail(
                    "4. API MONITORING", "Checked at UTC",
                    result.get("checked_at_utc"), "INFO",
                    "Timestamp recorded by the API monitoring agent."
                )
                if result.get("error"):
                    add_detail(
                        "4. API MONITORING", "Request error",
                        result.get("error"), "REVIEW",
                        "Investigate network, timeout or endpoint failure."
                    )

            elif agent_type == "infrastructure_agent":
                metrics = result.get("system_metrics") or {}
                add_detail(
                    "2. SERVER HEALTH", "Infrastructure health",
                    result.get("health"), status_for(result.get("health")),
                    f"Source report age: {result.get('report_age_minutes')} minutes; "
                    f"maximum allowed age: {result.get('report_max_age_minutes')} minutes."
                )
                for key, label in (
                    ("cpu_load_average_1_5_15m", "CPU load average (1 / 5 / 15 min)"),
                    ("memory_used_percent", "Memory usage (%)"),
                    ("swap_used_percent", "Swap usage (%)"),
                    ("root_disk_used_percent", "Root disk usage (%)"),
                ):
                    value = metrics.get(key)
                    add_detail(
                        "2. SERVER HEALTH", label, value,
                        "INFO" if value is not None else "MISSING",
                        "Value from the sanitized infrastructure snapshot; "
                        "not a newly sampled measurement."
                    )
                add_detail(
                    "2. SERVER HEALTH", "Source report generated at UTC",
                    result.get("report_generated_at_utc"), "INFO",
                    f"Source report age: {result.get('report_age_minutes')} minutes."
                )
                add_detail(
                    "3. DOCKER & APPLICATION SERVICES", "Container statistics",
                    result.get("container_stats"), "INFO",
                    "Raw sanitized container statistics returned by the infrastructure agent."
                )
                for container in result.get("containers") or []:
                    state = str(container.get("state", "unknown")).lower()
                    health = str(container.get("health", "unknown")).lower()
                    status = "PASS" if state == "running" and health not in ("unhealthy",) else "REVIEW"
                    add_detail(
                        "3. DOCKER & APPLICATION SERVICES",
                        container.get("name", "Unnamed container"),
                        container, status,
                        "Investigate containers not running or reporting unhealthy status."
                    )
                for item in result.get("log_summary") or []:
                    add_detail(
                        "5. POSTGRESQL & SERVICE LOGS",
                        f"Log summary — {item.get('service', 'unknown')}",
                        item, "REVIEW" if (item.get("error_like_line_count") or 0) else "INFO",
                        "Error-like log matches require review of raw lines and surrounding context."
                    )
                for item in result.get("findings") or []:
                    add_detail(
                        "5. POSTGRESQL & SERVICE LOGS",
                        "Infrastructure finding",
                        item.get("message"), status_for(item.get("severity")),
                        f"Severity: {item.get('severity')}; inspect source evidence."
                    )

            elif agent_type == "log_agent":
                add_detail(
                    "5. POSTGRESQL & SERVICE LOGS", "Log agent health",
                    result.get("health"), status_for(result.get("health")),
                    f"Services analyzed: {result.get('services_analyzed')}; "
                    f"report age: {result.get('report_age_minutes')} minutes."
                )
                add_detail(
                    "5. POSTGRESQL & SERVICE LOGS", "Severity counts",
                    result.get("severity_counts"), "INFO",
                    "Counts classify matching log lines; error-like matches are not proof of an incident."
                )
                add_detail(
                    "5. POSTGRESQL & SERVICE LOGS", "Failed services",
                    result.get("failed_services"), 
                    "REVIEW" if result.get("failed_services") else "PASS",
                    "Services for which log collection commands failed."
                )
                for evidence in result.get("evidence") or []:
                    add_detail(
                        "5. POSTGRESQL & SERVICE LOGS",
                        f"{evidence.get('service')} — {evidence.get('severity')}",
                        evidence.get("message"),
                        "INFO" if evidence.get("historical") else status_for(evidence.get("severity")),
                        f"Timestamp: {evidence.get('event_timestamp_utc')}; "
                        f"historical: {evidence.get('historical')}. "
                        "Historical evidence should not be treated as a current incident."
                    )
                for duplicate in result.get("duplicates") or []:
                    add_detail(
                        "5. POSTGRESQL & SERVICE LOGS", "Repeated log message",
                        duplicate, "INFO",
                        "Duplicate grouping is informational and does not indicate separate incidents."
                    )

            elif agent_type == "langfuse_agent":
                add_detail(
                    "6. LANGFUSE / RAG OBSERVABILITY", "Langfuse health",
                    result.get("health"), status_for(result.get("health")),
                    f"Observations analyzed: {result.get('observations_analyzed')} "
                    f"of {result.get('observations_requested')} requested."
                )
                add_detail(
                    "6. LANGFUSE / RAG OBSERVABILITY", "Severity counts",
                    result.get("severity_counts"), "INFO",
                    "Read-only observation analysis; prompts and outputs are excluded."
                )
                for observation in result.get("observations") or []:
                    metadata = observation.get("metadata") or {}
                    observation_status = (
                        "REVIEW" if observation.get("severity") in ("critical", "error")
                        else "INFO"
                    )
                    add_detail(
                        "6. LANGFUSE / RAG OBSERVABILITY",
                        f"Observation {observation.get('id') or 'unknown'}",
                        observation, observation_status,
                        f"Trace ID: {observation.get('trace_id')}; "
                        f"name: {observation.get('name')}; "
                        f"duration ms: {observation.get('duration_ms')}; "
                        f"retrieval error: {metadata.get('retrieval_error')}; "
                        f"retrieval count: {metadata.get('retrieval_count')}; "
                        f"timings: {metadata.get('timings_ms')}."
                    )

    for finding in findings:
        add_detail(
            "9. CONSOLIDATED FINDINGS",
            finding.get("agent", "Monitoring finding"),
            finding.get("message"),
            status_for(finding.get("severity")),
            f"Severity: {finding.get('severity')}; "
            f"historical: {finding.get('historical', False)}."
        )

    # ---------------- Read Me ----------------
    readme.append(["HOW TO READ THIS REPORT", "DESCRIPTION"])
    readme.append([])
    readme.append([
        "GREEN / PASS",
        "The relevant check passed based on the returned monitoring evidence."
    ])
    readme.append([
        "RED / REVIEW",
        "Investigation required. A warning or log match is not automatically a confirmed incident."
    ])
    readme.append([
        "GRAY / INFO",
        "Informational evidence or measurements; not a pass/fail judgement."
    ])
    readme.append([
        "MISSING",
        "Expected result or measurement was not available. Missing data is never treated as healthy."
    ])
    readme.append([
        "Snapshot timing",
        f"Report date: {dataset.get('report_date')}; timezone: {dataset.get('timezone')}; "
        f"coverage: {dataset.get('coverage_start')} to {dataset.get('coverage_end')}."
    ])
    readme.append([
        "Infrastructure limitation",
        "CPU load and memory, swap and disk percentages are included only when supplied by the sanitized infrastructure snapshot. "
        "Unavailable raw capacity metrics are not inferred."
    ])
    readme.append([
        "Log limitation",
        "Error-like log matches and historical messages are evidence for investigation, not proof of an active incident."
    ])
    readme.append([
        "Langfuse limitation",
        "The report contains whitelisted observation metadata. Prompts and outputs are excluded."
    ])
    readme.append([
        "Reporting completeness",
        "Observed cycle completion and expected-window completeness are separate measurements."
    ])
    readme.append([
        "Shared source",
        "Infrastructure and Log agents may analyze the same sanitized source report. Their evidence is identified by agent and should not be added together as independent source events."
    ])


    # ---------------- Formatting: approved reference dashboard ----------------
    from openpyxl.styles import Alignment, Border, Side

    navy = "203864"
    blue = "D9EAF7"
    green = "E2F0D9"
    red = "FCE4D6"
    grey = "E7E6E6"
    white = "FFFFFF"
    thin = Side(style="thin", color="D9E1F2")

    for sheet in (summary, detail, readme):
        sheet.sheet_view.showGridLines = False
        for row in sheet.iter_rows():
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cell.border = Border(bottom=thin)

    for cell in summary[1]:
        cell.fill = PatternFill("solid", fgColor=navy)
        cell.font = Font(name="Arial", size=22, bold=True, color=white)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for cell in summary[2]:
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.font = Font(name="Arial", size=15, bold=True, color=white)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for cell in summary[3]:
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
        cell.font = Font(name="Arial", size=10, bold=True, color=navy)
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    for col in ("A", "B", "C", "D"):
        summary.column_dimensions[col].width = 29
    summary.freeze_panes = "A5"
    summary.sheet_view.zoomScale = 90
    summary.page_setup.orientation = "portrait"
    summary.page_setup.fitToWidth = 1
    summary.page_setup.fitToHeight = 1
    summary.sheet_properties.pageSetUpPr.fitToPage = True
    summary.print_area = "A1:D22"

    for cell in detail[1]:
        cell.fill = PatternFill("solid", fgColor=navy)
        cell.font = Font(name="Arial", size=15, bold=True, color=white)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for cell in detail[2]:
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
        cell.font = Font(name="Arial", size=10, bold=True, color=navy)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for cell in detail[4]:
        cell.fill = PatternFill("solid", fgColor=navy)
        cell.font = Font(name="Arial", bold=True, color=white)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for row in range(5, detail.max_row + 1):
        section = detail.cell(row, 1).value
        if isinstance(section, str) and section[:1].isdigit() and section.isupper():
            for cell in detail[row]:
                cell.fill = PatternFill("solid", fgColor=blue)
                cell.font = Font(name="Arial", bold=True, color=navy)
        status = detail.cell(row, 4).value
        if status in ("PASS", "REVIEW", "INFO", "MISSING"):
            fill = {"PASS": green, "REVIEW": red, "INFO": grey, "MISSING": "FFF2CC"}[status]
            detail.cell(row, 4).fill = PatternFill("solid", fgColor=fill)
            detail.cell(row, 4).font = Font(name="Arial", bold=True)

    for index, width in enumerate([32, 34, 48, 14, 76], 1):
        detail.column_dimensions[get_column_letter(index)].width = width
    detail.freeze_panes = "A5"
    detail.auto_filter.ref = f"A4:E{detail.max_row}"
    detail.sheet_view.zoomScale = 80
    detail.page_setup.orientation = "landscape"
    detail.page_setup.fitToWidth = 1
    detail.page_setup.fitToHeight = 0
    detail.sheet_properties.pageSetUpPr.fitToPage = True
    detail.print_title_rows = "1:4"

    for cell in readme[1]:
        cell.fill = PatternFill("solid", fgColor=navy)
        cell.font = Font(name="Arial", bold=True, color=white)
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    readme.column_dimensions["A"].width = 25
    readme.column_dimensions["B"].width = 105
    readme.freeze_panes = "A2"
    readme.sheet_view.zoomScale = 90
    readme.page_setup.orientation = "portrait"
    readme.page_setup.fitToWidth = 1
    readme.sheet_properties.pageSetUpPr.fitToPage = True

    for sheet in (summary, detail, readme):
        sheet.sheet_properties.pageSetUpPr.fitToPage = True

    temporary = path.with_name(
        f".{path.stem}.{uuid.uuid4().hex}.tmp.xlsx"
    )
    try:
        workbook.save(temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()

def generate_daily_report(report_date=None, cutoff_hour=20):
    """Generate and persist one consolidated manager-ready Excel report."""
    report_date = report_date or datetime.now(IST).date()
    dataset = build_daily_dataset(report_date, cutoff_hour=cutoff_hour)

    output_dir = Path(
        os.getenv("REPORT_OUTPUT_DIR", "/var/lib/advanced-rag-reports")
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    excel_path = output_dir / (
        "Advanced_RAG_Agentic_AI_Daily_Observability_Report_"
        f"{report_date.isoformat()}.xlsx"
    )

    _write_excel(dataset, excel_path)

    validation = dataset["validation"]
    email_block_reasons = []

    if not validation["all_expected_cycles_present"]:
        email_block_reasons.append("Expected reporting windows are missing.")
    if not validation["all_observed_cycles_completed"]:
        email_block_reasons.append("One or more reporting cycles are incomplete.")
    if not validation["all_required_agents_completed"]:
        email_block_reasons.append("One or more required agent results are missing or failed.")
    if validation["has_cycle_mismatches"]:
        email_block_reasons.append("Reporting cycle metadata mismatches were detected.")

    report_status = "generated" if not email_block_reasons else "incomplete"

    report_id = rs.save_daily_report(
        report_id=str(uuid.uuid4()),
        report_date=report_date,
        dataset=dataset,
        html_path=None,
        excel_path=str(excel_path),
        json_path=None,
        validation=validation,
        status=report_status,
    )

    result = {
        "status": report_status,
        "report_date": report_date.isoformat(),
        "report_id": str(report_id),
        "excel_path": str(excel_path),
        "validation": validation,
    }

    if email_block_reasons:
        result["email"] = {
            "status": "blocked_validation",
            "reasons": email_block_reasons,
        }
    elif os.getenv("AGENT_REPORT_EMAIL_ENABLED", "false").strip().lower() == "true":
        try:
            from reporting.gmail_delivery import deliver_daily_report

            result["email"] = deliver_daily_report(
                report_date,
                str(excel_path),
            )
        except Exception as exc:
            result["email"] = {
                "status": "error",
                "error_type": type(exc).__name__,
            }
    else:
        result["email"] = {"status": "disabled"}

    return result


@app.task(name="monitoring.generate_daily_report")
def generate_daily_report_task():
    return generate_daily_report()
