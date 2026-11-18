
#!/usr/bin/env python3
"""
Create a sanitized monitoring report for Advanced RAG Agentic AI.

Source:
    /home/sirag/advanced-rag-monitoring/latest_report.json

Output:
    /home/sirag/advanced-rag-monitoring/safe-report/agent_report.json

This script only reads monitoring data and writes a sanitized JSON report.
It does not restart containers or modify application/database data.
"""

import json
import os
import tempfile
from pathlib import Path


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

REPORT_DIR = Path("/home/arun/Production/Monitoring")

SOURCE_FILE = REPORT_DIR / "latest_report.json"

OUTPUT_FILE = (
    REPORT_DIR / "safe-report" / "agent_report.json"
)


# ---------------------------------------------------------
# Read source JSON
# ---------------------------------------------------------

def load_json(path: Path) -> dict:
    """Load and validate a JSON object."""

    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected a JSON object in {path}"
        )

    return data


# ---------------------------------------------------------
# Build sanitized report
# ---------------------------------------------------------

def build_safe_report(report: dict) -> dict:
    """Keep approved fields and exclude raw logs."""

    api = report.get("api_health") or {}
    system = report.get("system_metrics") or {}
    docker = report.get("docker") or {}

    # Container status only; no raw container logs.
    safe_containers = []

    for container in docker.get("containers", []):
        if not isinstance(container, dict):
            continue

        safe_containers.append({
            "service": container.get("service"),
            "name": container.get("name"),
            "state": container.get("state"),
            "status": container.get("status"),
            "health": container.get("health"),
        })

    # Resource usage only.
    safe_stats = []

    for stat in docker.get("stats", []):
        if not isinstance(stat, dict):
            continue

        safe_stats.append({
            "container": stat.get("container"),
            "cpu_percent": stat.get("cpu_percent"),
            "memory_usage": stat.get("memory_usage"),
            "memory_percent": stat.get("memory_percent"),
        })

    # Include only counts and command status from log summaries.
    safe_log_summary = []

    for item in report.get("log_summary", []):
        if not isinstance(item, dict):
            continue

        matching_lines = item.get("recent_matching_lines") or []

        # Keep only bounded, already-redacted log evidence.
        safe_matching_lines = [
            str(line)[:400]
            for line in matching_lines[-10:]
            if isinstance(line, str)
        ]

        safe_log_summary.append({
            "service": str(item.get("service") or "unknown")[:40],
            "command_ok": bool(item.get("command_ok", False)),
            "lines_scanned": int(item.get("lines_scanned") or 0),
            "error_like_line_count":
                int(item.get("error_like_line_count") or 0),
            "recent_matching_lines": safe_matching_lines,
        })

    warnings = [
        str(warning)
        for warning in (report.get("warnings") or [])
    ]

    # Detect container issues even if the source monitor
    # did not add them to its warnings list.
    for container in safe_containers:
        service = container.get("service", "unknown")
        state = str(container.get("state") or "").lower()
        health = str(container.get("health") or "").lower()

        if state != "running":
            warnings.append(
                f"Container {service} is not running."
            )
        elif health == "unhealthy":
            warnings.append(
                f"Container {service} reports unhealthy."
            )

    # Deduplicate while preserving the original order.
    warnings = list(dict.fromkeys(warnings))

    return {
        "generated_at_utc": report.get("generated_at_utc"),
        "project": "Advanced RAG Agentic AI",
        "mode": "read-only",

        "api_health": {
            "ok": bool(api.get("ok", False)),
            "http_status": api.get("http_status"),
        },

        "system_metrics": {
            "cpu_load_average_1_5_15m":
                system.get("cpu_load_average_1_5_15m"),
            "memory_total_bytes":
                system.get("memory_total_bytes"),
            "memory_available_bytes":
                system.get("memory_available_bytes"),
            "memory_used_percent":
                system.get("memory_used_percent"),
            "swap_used_percent":
                system.get("swap_used_percent"),
            "root_disk_total_bytes":
                system.get("root_disk_total_bytes"),
            "root_disk_used_bytes":
                system.get("root_disk_used_bytes"),
            "root_disk_free_bytes":
                system.get("root_disk_free_bytes"),
            "root_disk_used_percent":
                system.get("root_disk_used_percent"),
        },

        "docker": {
            "compose_command_ok":
                bool(docker.get("compose_command_ok", False)),
            "containers": safe_containers,
            "stats_command_ok":
                bool(docker.get("stats_command_ok", False)),
            "stats": safe_stats,
        },

        "log_summary": safe_log_summary,

        "warnings": warnings,

        "note": (
            "Sanitized monitoring snapshot. Log matches are not "
            "proof of a current failure. No services or application "
            "data were changed by this script."
        ),
    }


# ---------------------------------------------------------
# Atomic JSON writer
# ---------------------------------------------------------

def atomic_write_json(path: Path, data: dict) -> None:
    """
    Write a temporary file in the destination directory,
    then atomically replace the destination file.

    Mounting the parent directory into Docker allows the
    container to see subsequent atomic file replacements.
    """

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o755,
    )

    fd, temporary_name = tempfile.mkstemp(
        prefix=".agent_report_",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )

    try:
        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(
                data,
                file,
                indent=2,
                ensure_ascii=False,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        os.chmod(temporary_name, 0o644)
        os.replace(temporary_name, path)

    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main() -> None:
    if not SOURCE_FILE.is_file():
        raise FileNotFoundError(
            f"Source report not found: {SOURCE_FILE}\n"
            "Run monitoring/monitor_server.py first."
        )

    source_report = load_json(SOURCE_FILE)

    safe_report = build_safe_report(source_report)

    atomic_write_json(
        OUTPUT_FILE,
        safe_report,
    )

    print("Sanitized report created successfully.")
    print(f"Output: {OUTPUT_FILE}")
    print(
        "Timestamp:",
        safe_report.get("generated_at_utc"),
    )
    print(
        "API health:",
        safe_report.get("api_health"),
    )
    print(
        "Container count:",
        len(safe_report["docker"]["containers"]),
    )
    print(
        "Warnings:",
        len(safe_report["warnings"]),
    )


if __name__ == "__main__":
    main()
