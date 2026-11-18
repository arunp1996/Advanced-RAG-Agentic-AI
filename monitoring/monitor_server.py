#!/usr/bin/env python3
"""Read-only monitoring for the Advanced RAG Docker Compose stack."""

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(
    "/home/arun/Production/Advanced_RAG_Agentic_AI"
)
COMPOSE = PROJECT / "deploy/compose/docker-compose.yml"
REPORT_DIR = Path("/home/arun/Production/Monitoring")
REPORT_FILE = REPORT_DIR / "latest_report.json"

SERVICES = ("api", "frontend", "postgres")
SECRET_PATTERNS = [
    (re.compile(r"(?i)(password|passwd|token|api[_-]?key|secret)"
                r"\s*[:=]\s*[^,\s]+"), r"\1=[REDACTED]"),
]


def run_command(args, timeout=10):
    """Run a read-only command and return bounded output."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout[:20000],
            "stderr": result.stderr[:2000],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": -1, "stdout": "", "stderr": str(exc)}


def redact(text):
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def read_api_health():
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:8001/health",
            headers={"User-Agent": "advanced-rag-monitor/1.0"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            body = response.read(2000).decode("utf-8", errors="replace")
            return {
                "ok": response.status == 200,
                "http_status": response.status,
                "body": body,
            }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "error": str(exc)[:300]}


def read_system_metrics():
    disk = shutil.disk_usage("/")
    memory = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as file:
            for line in file:
                key, value = line.split(":", 1)
                if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    memory[key] = int(value.strip().split()[0]) * 1024
    except (OSError, ValueError):
        memory = {"error": "Could not read /proc/meminfo"}

    try:
        with open("/proc/loadavg", encoding="utf-8") as file:
            load_average = file.read().strip().split()[:3]
    except OSError:
        load_average = []

    mem_total = memory.get("MemTotal", 0)
    mem_available = memory.get("MemAvailable", 0)
    swap_total = memory.get("SwapTotal", 0)
    swap_free = memory.get("SwapFree", 0)

    disk_percent = round(disk.used / disk.total * 100, 1) if disk.total else 0
    memory_percent = (
        round((mem_total - mem_available) / mem_total * 100, 1)
        if mem_total else None
    )
    swap_percent = (
        round((swap_total - swap_free) / swap_total * 100, 1)
        if swap_total else 0
    )

    return {
        "cpu_load_average_1_5_15m": load_average,
        "memory_total_bytes": mem_total,
        "memory_available_bytes": mem_available,
        "memory_used_percent": memory_percent,
        "swap_used_percent": swap_percent,
        "root_disk_total_bytes": disk.total,
        "root_disk_used_bytes": disk.used,
        "root_disk_free_bytes": disk.free,
        "root_disk_used_percent": disk_percent,
    }


def read_container_status():
    result = run_command([
        "docker", "compose", "-p", "ragdashboardv2", "--env-file", str(PROJECT / ".env"), "-f", str(COMPOSE),
        "ps", "--format", "json",
    ])
    containers = []
    if result["returncode"] == 0:
        for line in result["stdout"].splitlines():
            try:
                item = json.loads(line)
                containers.append({
                    "service": item.get("Service", "unknown"),
                    "name": item.get("Name", "unknown"),
                    "state": item.get("State", "unknown"),
                    "status": item.get("Status", "unknown"),
                    "health": item.get("Health", ""),
                })
            except json.JSONDecodeError:
                continue

    stats_result = run_command([
        "docker", "stats", "--no-stream", "--format",
        "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}",
    ])
    stats = []
    if stats_result["returncode"] == 0:
        for line in stats_result["stdout"].splitlines():
            parts = line.split("|")
            if len(parts) == 4:
                stats.append({
                    "container": parts[0],
                    "cpu_percent": parts[1],
                    "memory_usage": parts[2],
                    "memory_percent": parts[3],
                })

    return {
        "compose_command_ok": result["returncode"] == 0,
        "containers": containers,
        "stats_command_ok": stats_result["returncode"] == 0,
        "stats": stats,
        "errors": redact(result["stderr"] + stats_result["stderr"])[:1000],
    }


def read_log_summary(service, tail=100):
    result = run_command([
        "docker", "compose", "-p", "ragdashboardv2", "--env-file", str(PROJECT / ".env"), "-f", str(COMPOSE),
        "logs", "--no-color", "--tail", str(tail), service,
    ], timeout=15)

    # Store only matching error-like lines, not entire logs.
    patterns = re.compile(
        r"\b(error|exception|traceback|critical|fatal|failed|failure)\b",
        re.IGNORECASE,
    )
    matches = [
        redact(line)[:400]
        for line in result["stdout"].splitlines()
        if patterns.search(line)
    ]

    return {
        "service": service,
        "command_ok": result["returncode"] == 0,
        "lines_scanned": len(result["stdout"].splitlines()),
        "error_like_line_count": len(matches),
        "recent_matching_lines": matches[-10:],
        "error": redact(result["stderr"])[:500],
    }


def main():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    system = read_system_metrics()
    api = read_api_health()
    docker = read_container_status()
    logs = [read_log_summary(service) for service in SERVICES]

    warnings = []
    if not api.get("ok"):
        warnings.append("API health check failed")
    if system.get("root_disk_used_percent", 0) >= 90:
        warnings.append("Root disk usage is critical (90% or higher)")
    elif system.get("root_disk_used_percent", 0) >= 80:
        warnings.append("Root disk usage is high (80% or higher)")
    if (system.get("memory_used_percent") or 0) >= 90:
        warnings.append("Memory usage is high (90% or higher)")
    if not docker["compose_command_ok"]:
        warnings.append("Docker Compose status check failed")

    for container in docker["containers"]:
        state = str(container.get("state", "")).lower()
        if state not in ("running", "up"):
            warnings.append(
                f"Container {container['name']} is not running: "
                f"{container.get('status', state)}"
            )

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "project": "Advanced RAG Agentic AI",
        "mode": "read-only",
        "api_health": api,
        "system_metrics": system,
        "docker": docker,
        "log_summary": logs,
        "warnings": warnings,
        "note": (
            "Error-like log matches require review; they are not proof "
            "of a current failure. No services or data were changed."
        ),
    }

    temporary = REPORT_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(REPORT_FILE)
    os.chmod(REPORT_FILE, 0o600)

    print(json.dumps(report, indent=2))
    print(f"\nReport saved to: {REPORT_FILE}")


if __name__ == "__main__":
    main()
