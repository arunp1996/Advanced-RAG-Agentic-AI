from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.request import urlopen
from urllib.error import HTTPError, URLError
import json
import os
import re
import time

from celery_app import app
import monitoring_store as store


@app.task(name="monitoring.smoke_check")
def smoke_check():
    return {
        "status": "success",
        "task": "monitoring.smoke_check",
        "timestamp_ist": datetime.now(
            ZoneInfo("Asia/Kolkata")
        ).isoformat(),
        "message": "Isolated Celery monitoring scheduler is operational.",
    }


@app.task(name="monitoring.daily_smoke_check")
def daily_smoke_check():
    return {
        "status": "success",
        "task": "monitoring.daily_smoke_check",
        "timestamp_ist": datetime.now(
            ZoneInfo("Asia/Kolkata")
        ).isoformat(),
        "message": "Daily scheduler smoke test executed.",
    }


@app.task(
    bind=True,
    name="monitoring.api_monitor",
    acks_late=True,
)
def api_monitor(self, task_id):
    """Monitor the isolated API and persist its result in agent_tasks."""

    task = store.get_task(task_id)
    if task is None:
        raise RuntimeError("Agent task does not exist.")

    if self.request.id != task_id:
        raise RuntimeError("Celery task ID does not match AgentTask ID.")

    store.update_task(
        task_id,
        status="running",
        progress=5,
        started_at=store.utc_now(),
        finished_at=None,
        error_summary=None,
    )
    store.add_log(task_id, "API Monitor started.")

    try:
        if not store.checkpoint(task_id):
            return {"status": "cancelled"}

        url = os.environ.get("AGENTIC_SELF_HEALTH_URL")
        if not url:
            raise RuntimeError("API health URL is not configured.")

        started = time.monotonic()

        try:
            with urlopen(url, timeout=5) as response:
                http_status = response.status
                body = response.read(4096).decode(
                    "utf-8", errors="replace"
                )
                ok = 200 <= http_status < 300

        except HTTPError as exc:
            http_status = exc.code
            body = exc.read(4096).decode(
                "utf-8", errors="replace"
            )
            ok = False

        except (URLError, TimeoutError, OSError) as exc:
            http_status = None
            body = ""
            ok = False
            request_error = str(exc)[:500]
        else:
            request_error = None

        elapsed_ms = round((time.monotonic() - started) * 1000, 2)

        if not store.checkpoint(task_id):
            return {"status": "cancelled"}

        result = {
            "url": url,
            "ok": ok,
            "http_status": http_status,
            "response_excerpt": body[:1000],
            "response_time_ms": elapsed_ms,
            "error": request_error,
            "checked_at_utc": store.utc_now().isoformat(),
        }

        store.update_task(
            task_id,
            status="completed",
            progress=100,
            result=result,
            finished_at=store.utc_now(),
        )

        store.add_log(
            task_id,
            "API health check completed: "
            + ("healthy." if ok else "unhealthy."),
            "info" if ok else "warning",
        )

        return result

    except Exception as exc:
        store.update_task(
            task_id,
            status="failed",
            error_summary=str(exc)[:1000],
            finished_at=store.utc_now(),
        )
        store.add_log(
            task_id,
            "API Monitor execution failed: " + str(exc)[:500],
            "error",
        )
        raise


@app.task(
    bind=True,
    name="monitoring.infrastructure_agent",
    acks_late=True,
)
def infrastructure_agent(self, task_id):
    """Analyze the sanitized, read-only host infrastructure report."""

    task = store.get_task(task_id)
    if task is None:
        raise RuntimeError("Agent task does not exist.")

    if self.request.id != task_id:
        raise RuntimeError("Celery task ID does not match AgentTask ID.")

    store.update_task(
        task_id,
        status="running",
        progress=5,
        started_at=store.utc_now(),
        finished_at=None,
        error_summary=None,
    )
    store.add_log(task_id, "Infrastructure Agent started.")

    try:
        if not store.checkpoint(task_id):
            return {"status": "cancelled"}

        report_path = os.environ.get(
            "INFRASTRUCTURE_REPORT_PATH",
            "/run/advanced-rag-monitoring/agent_report.json",
        )

        if not os.path.isfile(report_path):
            raise RuntimeError("Infrastructure report file does not exist.")

        if os.path.getsize(report_path) > 5 * 1024 * 1024:
            raise RuntimeError("Infrastructure report exceeds 5 MiB.")

        with open(report_path, "r", encoding="utf-8") as report_file:
            report = json.load(report_file)

        if not isinstance(report, dict):
            raise RuntimeError("Infrastructure report must be a JSON object.")

        required_sections = {
            "generated_at_utc",
            "system_metrics",
            "docker",
            "log_summary",
            "warnings",
        }

        missing = required_sections - report.keys()
        if missing:
            raise RuntimeError(
                "Infrastructure report missing required sections: "
                + ", ".join(sorted(missing))
            )

        if not store.checkpoint(task_id):
            return {"status": "cancelled"}

        generated_at = datetime.fromisoformat(
            report["generated_at_utc"].replace("Z", "+00:00")
        )

        if generated_at.tzinfo is None:
            raise RuntimeError("Report timestamp must include a timezone.")

        report_age_minutes = round(
            (store.utc_now() - generated_at).total_seconds() / 60,
            2,
        )

        metrics = report["system_metrics"]
        docker_data = report["docker"]
        containers = docker_data.get("containers") or []
        stats = docker_data.get("stats") or []
        log_summary = report.get("log_summary") or []

        findings = []
        critical_count = 0
        warning_count = 0

        def add_finding(severity, message):
            nonlocal critical_count, warning_count

            findings.append({
                "severity": severity,
                "message": message,
            })

            if severity == "critical":
                critical_count += 1
            elif severity == "warning":
                warning_count += 1

        # Report freshness: default maximum age is 30 minutes.
        max_age = int(
            os.environ.get("INFRASTRUCTURE_REPORT_MAX_AGE_MINUTES", "30")
        )

        if report_age_minutes < 0:
            add_finding(
                "warning",
                "Report timestamp is in the future.",
            )
        elif report_age_minutes > max_age:
            add_finding(
                "warning",
                f"Report is stale: {report_age_minutes} minutes old.",
            )

        # Memory and disk thresholds.
        memory_used = metrics.get("memory_used_percent")
        if memory_used is not None:
            memory_used = float(memory_used)

            if memory_used >= 95:
                add_finding("critical", f"Memory usage is high: {memory_used}%.")
            elif memory_used >= 90:
                add_finding("warning", f"Memory usage is elevated: {memory_used}%.")

        disk_used = metrics.get("root_disk_used_percent")
        if disk_used is not None:
            disk_used = float(disk_used)

            if disk_used >= 95:
                add_finding("critical", f"Root disk usage is high: {disk_used}%.")
            elif disk_used >= 85:
                add_finding("warning", f"Root disk usage is elevated: {disk_used}%.")

        swap_used = metrics.get("swap_used_percent")
        if swap_used is not None:
            swap_used = float(swap_used)

            if swap_used >= 90:
                add_finding("critical", f"Swap usage is high: {swap_used}%.")
            elif swap_used >= 80:
                add_finding("warning", f"Swap usage is elevated: {swap_used}%.")

        # Docker command and container health.
        if not docker_data.get("compose_command_ok", False):
            add_finding("warning", "Docker Compose inspection failed.")

        if not docker_data.get("stats_command_ok", False):
            add_finding("warning", "Docker statistics inspection failed.")

        for container in containers:
            name = str(container.get("name", "unknown"))
            state = str(container.get("state", "")).lower()
            health = str(container.get("health", "")).lower()

            if state != "running":
                add_finding(
                    "critical",
                    f"Container {name} is not running: {state or 'unknown'}.",
                )
            elif health == "unhealthy":
                add_finding(
                    "critical",
                    f"Container {name} reports unhealthy status.",
                )
            elif health == "starting":
                add_finding(
                    "warning",
                    f"Container {name} health check is still starting.",
                )

        # Log matches are indicators for investigation, not proof of failure.
        for item in log_summary:
            service = str(item.get("service", "unknown"))
            count = int(item.get("error_like_line_count") or 0)

            if not item.get("command_ok", False):
                add_finding(
                    "warning",
                    f"Log inspection failed for {service}.",
                )
            elif count > 0:
                add_finding(
                    "warning",
                    f"{service} logs contain {count} error-like lines; "
                    "review required.",
                )

        for warning in report.get("warnings") or []:
            add_finding("warning", str(warning)[:500])

        if not containers:
            add_finding(
                "warning",
                "No container information was present in the report.",
            )

        if critical_count:
            health = "critical"
        elif warning_count:
            health = "warning"
        else:
            health = "healthy"

        if not store.checkpoint(task_id):
            return {"status": "cancelled"}

        result = {
            "health": health,
            "report_generated_at_utc": generated_at.isoformat(),
            "report_age_minutes": report_age_minutes,
            "report_max_age_minutes": max_age,
            "project": report.get("project"),
            "mode": report.get("mode"),
            "system_metrics": {
                "cpu_load_average_1_5_15m": metrics.get(
                    "cpu_load_average_1_5_15m"
                ),
                "memory_used_percent": memory_used,
                "swap_used_percent": swap_used,
                "root_disk_used_percent": disk_used,
            },
            "containers": containers,
            "container_stats": stats,
            "log_summary": log_summary,
            "findings": findings,
            "critical_count": critical_count,
            "warning_count": warning_count,
            "analyzed_at_utc": store.utc_now().isoformat(),
        }

        store.update_task(
            task_id,
            status="completed",
            progress=100,
            result=result,
            finished_at=store.utc_now(),
        )

        store.add_log(
            task_id,
            f"Infrastructure analysis completed: {health}. "
            f"Critical findings: {critical_count}; "
            f"warnings: {warning_count}.",
            "error" if critical_count else (
                "warning" if warning_count else "info"
            ),
        )

        return result

    except Exception as exc:
        store.update_task(
            task_id,
            status="failed",
            error_summary=str(exc)[:1000],
            finished_at=store.utc_now(),
        )
        store.add_log(
            task_id,
            "Infrastructure Agent execution failed: " + str(exc)[:500],
            "error",
        )
        raise


@app.task(
    bind=True,
    name="monitoring.log_agent",
    acks_late=True,
)
def log_agent(self, task_id):
    """Analyze sanitized service logs and preserve bounded evidence."""

    task = store.get_task(task_id)
    if task is None:
        raise RuntimeError("Agent task does not exist.")

    if self.request.id != task_id:
        raise RuntimeError("Celery task ID does not match AgentTask ID.")

    store.update_task(
        task_id,
        status="running",
        progress=5,
        started_at=store.utc_now(),
        finished_at=None,
        error_summary=None,
    )
    store.add_log(task_id, "Log Agent started.")

    try:
        if not store.checkpoint(task_id):
            return {"status": "cancelled"}

        report_path = os.environ.get(
            "INFRASTRUCTURE_REPORT_PATH",
            "/run/advanced-rag-monitoring/agent_report.json",
        )

        if not os.path.isfile(report_path):
            raise RuntimeError("Sanitized monitoring report does not exist.")

        if os.path.getsize(report_path) > 5 * 1024 * 1024:
            raise RuntimeError("Monitoring report exceeds 5 MiB.")

        with open(report_path, "r", encoding="utf-8") as report_file:
            report = json.load(report_file)

        if not isinstance(report, dict):
            raise RuntimeError("Monitoring report must be a JSON object.")

        generated_at = datetime.fromisoformat(
            report["generated_at_utc"].replace("Z", "+00:00")
        )

        if generated_at.tzinfo is None:
            raise RuntimeError("Report timestamp must include a timezone.")

        log_summary = report.get("log_summary")
        if not isinstance(log_summary, list):
            raise RuntimeError("Report log_summary must be a list.")

        now = store.utc_now()
        report_age_minutes = round(
            (now - generated_at).total_seconds() / 60, 2
        )
        max_age = int(
            os.environ.get("LOG_AGENT_REPORT_MAX_AGE_MINUTES", "30")
        )
        historical_hours = int(
            os.environ.get("LOG_AGENT_HISTORICAL_HOURS", "24")
        )

        findings = []
        evidence = []
        severity_counts = {
            "critical": 0,
            "error": 0,
            "warning": 0,
            "info": 0,
        }
        duplicate_counts = {}
        failed_services = []

        def classify(message):
            value = message.lower()

            if any(word in value for word in (
                "out of memory", "oomkilled", "kernel panic",
                "segmentation fault", "critical",
            )):
                return "critical"

            if any(word in value for word in (
                "fatal:", "exception", "traceback",
                " error:", " error ", "failed", "failure",
            )):
                return "error"

            if any(word in value for word in (
                "warning:", " warn ", "deprecated",
            )):
                return "warning"

            return "info"

        timestamp_pattern = re.compile(
            r"(\d{4}-\d{2}-\d{2} "
            r"\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
        )

        for index, item in enumerate(log_summary):
            if not store.checkpoint(task_id):
                return {"status": "cancelled"}

            if not isinstance(item, dict):
                continue

            service = str(item.get("service") or "unknown")[:40]

            if not item.get("command_ok", False):
                failed_services.append(service)
                findings.append({
                    "severity": "warning",
                    "service": service,
                    "message": "Log collection command failed.",
                })
                continue

            lines = item.get("recent_matching_lines") or []
            if not isinstance(lines, list):
                raise RuntimeError(
                    f"Log evidence for {service} must be a list."
                )

            for raw_line in lines[-10:]:
                if not isinstance(raw_line, str):
                    continue

                line = raw_line[:400]
                severity = classify(line)
                severity_counts[severity] += 1

                timestamp_match = timestamp_pattern.search(line)
                event_time = None
                historical = True

                if timestamp_match:
                    timestamp_text = timestamp_match.group(1)
                    try:
                        try:
                            event_time = datetime.strptime(
                                timestamp_text, "%Y-%m-%d %H:%M:%S.%f"
                            )
                        except ValueError:
                            event_time = datetime.strptime(
                                timestamp_text, "%Y-%m-%d %H:%M:%S"
                            )

                        from datetime import timezone
                        event_time = event_time.replace(tzinfo=timezone.utc)
                        historical = (
                            (now - event_time).total_seconds()
                            > historical_hours * 3600
                        )
                    except ValueError:
                        event_time = None

                # Group repeated messages without timestamp,
                # container prefix, or PostgreSQL process ID.
                fingerprint = timestamp_pattern.sub("", line, count=1)
                fingerprint = re.sub(r"\[\d+\]", "", fingerprint)
                fingerprint = re.sub(
                    r"^\s*[^|]+\|\s*", "", fingerprint
                )
                normalized = " ".join(fingerprint.lower().split())

                duplicate_counts[normalized] = (
                    duplicate_counts.get(normalized, 0) + 1
                )

                evidence.append({
                    "service": service,
                    "severity": severity,
                    "event_timestamp_utc": (
                        event_time.isoformat() if event_time else None
                    ),
                    "historical": historical,
                    "message": line,
                })

                findings.append({
                    "severity": severity,
                    "service": service,
                    "historical": historical,
                    "message": line,
                })

            store.update_task(
                task_id,
                progress=min(85, 15 + int(
                    70 * (index + 1) / max(len(log_summary), 1)
                )),
            )

        duplicates = [
            {"message": message[:400], "count": count}
            for message, count in duplicate_counts.items()
            if count > 1
        ]

        if report_age_minutes < 0 or report_age_minutes > max_age:
            findings.append({
                "severity": "warning",
                "message": (
                    f"Monitoring report is outside the allowed freshness "
                    f"window: {report_age_minutes} minutes."
                ),
            })

        if failed_services:
            findings.append({
                "severity": "warning",
                "message": "Log collection failed for: "
                + ", ".join(failed_services),
            })

        critical_count = severity_counts["critical"]
        error_count = severity_counts["error"]
        warning_count = severity_counts["warning"]

        if critical_count:
            health = "critical"
        elif error_count or warning_count or failed_services:
            health = "warning"
        else:
            health = "healthy"

        if report_age_minutes < 0 or report_age_minutes > max_age:
            health = "warning"

        if not store.checkpoint(task_id):
            return {"status": "cancelled"}

        result = {
            "health": health,
            "report_generated_at_utc": generated_at.isoformat(),
            "report_age_minutes": report_age_minutes,
            "report_max_age_minutes": max_age,
            "historical_threshold_hours": historical_hours,
            "services_analyzed": len(log_summary),
            "evidence_count": len(evidence),
            "severity_counts": severity_counts,
            "critical_count": critical_count,
            "error_count": error_count,
            "warning_count": warning_count,
            "failed_services": failed_services,
            "duplicates": duplicates,
            "findings": findings,
            "evidence": evidence,
            "analyzed_at_utc": now.isoformat(),
        }

        store.update_task(
            task_id,
            status="completed",
            progress=100,
            result=result,
            finished_at=store.utc_now(),
        )

        store.add_log(
            task_id,
            f"Log analysis completed: {health}. "
            f"Evidence: {len(evidence)}; "
            f"critical: {critical_count}; "
            f"errors: {error_count}; warnings: {warning_count}.",
            "error" if critical_count else (
                "warning" if error_count or warning_count else "info"
            ),
        )

        return result

    except Exception as exc:
        store.update_task(
            task_id,
            status="failed",
            error_summary=str(exc)[:1000],
            finished_at=store.utc_now(),
        )
        store.add_log(
            task_id,
            "Log Agent execution failed: " + str(exc)[:500],
            "error",
        )
        raise

@app.task(
    bind=True,
    name="monitoring.langfuse_agent",
    acks_late=True,
)
def langfuse_agent(self, task_id):
    """Read-only Langfuse observation analysis with task lifecycle support."""

    store.update_task(
        task_id,
        status="running",
        progress=5,
        started_at=store.utc_now(),
        error_summary=None,
    )
    store.add_log(task_id, "Langfuse Agent started.")

    try:
        if not store.checkpoint(task_id):
            return {"status": "cancelled"}

        store.update_task(task_id, progress=15)
        store.add_log(task_id, "Connecting to Langfuse observations.")

        from langfuse import get_client

        client = get_client()

        store.update_task(task_id, progress=30)

        response = client.api.observations.get_many(
            limit=100,
            fields="core,basic,time,metadata",
        )

        observations = getattr(response, "data", None)

        if observations is None and isinstance(response, dict):
            observations = response.get("data", [])

        observations = observations or []

        if not isinstance(observations, (list, tuple)):
            raise ValueError("Unexpected Langfuse observations response.")

        if not store.checkpoint(task_id):
            return {"status": "cancelled"}

        safe_observations = []
        findings = []
        severity_counts = {
            "critical": 0,
            "error": 0,
            "info": 0,
        }

        def get_value(item, name, default=None):
            if isinstance(item, dict):
                return item.get(name, default)
            return getattr(item, name, default)

        def safe_text(value, limit=160):
            if value is None:
                return None
            return str(value)[:limit]

        for index, observation in enumerate(observations):
            if index % 10 == 0:
                if not store.checkpoint(task_id):
                    return {"status": "cancelled"}

                store.update_task(
                    task_id,
                    progress=min(
                        85,
                        35 + int(50 * index / max(len(observations), 1)),
                    ),
                )

            level_object = get_value(observation, "level", "")
            level = str(
                getattr(level_object, "value", level_object) or ""
            ).lower()

            metadata = get_value(observation, "metadata") or {}

            if not isinstance(metadata, dict):
                metadata = {}

            retrieval_error = bool(metadata.get("retrieval_error"))

            if level == "fatal":
                severity = "critical"
            elif level == "error" or retrieval_error:
                severity = "error"
            else:
                severity = "info"

            severity_counts[severity] += 1

            timings = metadata.get("timings_ms")
            safe_timings = {}

            if isinstance(timings, dict):
                for key, value in list(timings.items())[:12]:
                    if isinstance(value, (int, float)) and not isinstance(
                        value, bool
                    ):
                        safe_timings[str(key)[:60]] = value

            retrieval_count = metadata.get("retrieval_count")
            attempts = metadata.get("attempts")

            safe_metadata = {
                "retrieval_error": retrieval_error,
                "evidence_found": (
                    bool(metadata.get("evidence_found"))
                    if "evidence_found" in metadata else None
                ),
                "retrieval_count": (
                    retrieval_count
                    if isinstance(retrieval_count, int)
                    and not isinstance(retrieval_count, bool)
                    else None
                ),
                "attempts": (
                    attempts
                    if isinstance(attempts, int)
                    and not isinstance(attempts, bool)
                    else None
                ),
                "timings_ms": safe_timings,
            }

            observation_data = {
                "id": safe_text(get_value(observation, "id"), 80),
                "trace_id": safe_text(
                    get_value(observation, "trace_id"), 80
                ),
                "name": safe_text(get_value(observation, "name")),
                "type": safe_text(get_value(observation, "type")),
                "level": level[:20],
                "start_time": safe_text(
                    get_value(observation, "start_time"), 64
                ),
                "duration_ms": (
                    get_value(observation, "duration_ms")
                    if isinstance(
                        get_value(observation, "duration_ms"),
                        (int, float),
                    )
                    else None
                ),
                "status_message_present": bool(
                    get_value(observation, "status_message")
                ),
                "severity": severity,
                "metadata": safe_metadata,
            }

            safe_observations.append(observation_data)

            if severity in {"critical", "error"}:
                findings.append({
                    "severity": severity,
                    "observation_id": observation_data["id"],
                    "trace_id": observation_data["trace_id"],
                    "name": observation_data["name"],
                    "level": observation_data["level"],
                    "retrieval_error": retrieval_error,
                    "start_time": observation_data["start_time"],
                })

        if not store.checkpoint(task_id):
            return {"status": "cancelled"}

        critical_count = severity_counts["critical"]
        error_count = severity_counts["error"]

        if critical_count:
            health = "critical"
        elif error_count:
            health = "warning"
        else:
            health = "healthy"

        result = {
            "health": health,
            "source": "langfuse",
            "observations_requested": 100,
            "observations_analyzed": len(safe_observations),
            "severity_counts": severity_counts,
            "critical_count": critical_count,
            "error_count": error_count,
            "findings_count": len(findings),
            "findings": findings[:100],
            "observations": safe_observations[:100],
            "analyzed_at_utc": store.utc_now().isoformat(),
            "note": (
                "Read-only observation analysis. Only whitelisted "
                "metadata is retained; prompts and outputs are excluded."
            ),
        }

        if not store.checkpoint(task_id):
            return {"status": "cancelled"}

        store.update_task(
            task_id,
            status="completed",
            progress=100,
            result=result,
            finished_at=store.utc_now(),
        )

        store.add_log(
            task_id,
            f"Langfuse analysis completed: {health}. "
            f"Observations: {len(safe_observations)}; "
            f"critical: {critical_count}; errors: {error_count}.",
            "warning" if critical_count or error_count else "info",
        )

        return result

    except Exception as exc:
        store.update_task(
            task_id,
            status="failed",
            error_summary=f"{type(exc).__name__}: {str(exc)[:800]}",
            finished_at=store.utc_now(),
        )
        store.add_log(
            task_id,
            f"Langfuse Agent execution failed: "
            f"{type(exc).__name__}: {str(exc)[:400]}",
            "error",
        )
        raise
