"""Admin-only, approval-gated monitoring tasks for Advanced RAG Agentic AI.

Execution is intentionally in-process for this first safe implementation.
Use one API worker only until a durable external worker/queue is configured.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, select
from sqlalchemy.sql import func
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, SessionLocal, get_db
from .models import AuditEvent
from .security import admin_user

router = APIRouter(prefix="/api/admin/agent-tasks", tags=["admin-agent-tasks"])

TERMINAL = {"completed", "failed", "cancelled"}
ALLOWED_TYPES = {"api_monitor", "log_analysis", "langfuse_report", "daily_report"}
_registry_lock = threading.Lock()
_workers: dict[str, threading.Thread] = {}


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    requested_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    logs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TaskCreate(BaseModel):
    agent_type: Literal["api_monitor", "log_analysis", "langfuse_report", "daily_report"]
    params: dict[str, Any] = Field(default_factory=dict)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def _task_dict(task: AgentTask) -> dict[str, Any]:
    return {
        "id": task.id, "agent_type": task.agent_type, "status": task.status,
        "progress": task.progress, "params": task.params or {}, "result": task.result,
        "logs": task.logs or [], "error_summary": task.error_summary,
        "created_at": _iso(task.created_at), "started_at": _iso(task.started_at),
        "finished_at": _iso(task.finished_at), "updated_at": _iso(task.updated_at),
    }


def _audit(db, admin, action: str, task: AgentTask, extra: dict[str, Any] | None = None) -> None:
    details = {"task_id": task.id, "agent_type": task.agent_type, "status": task.status}
    if extra:
        details.update(extra)
    db.add(AuditEvent(actor_user_id=admin.id, action=action, details=details))


def _mutate(task_id: str, callback) -> None:
    db = SessionLocal()
    try:
        task = db.get(AgentTask, task_id)
        if task is None:
            return
        callback(task)
        db.commit()
    finally:
        db.close()


def _log(task_id: str, message: str, level: str = "info") -> None:
    entry = {"at": datetime.now(timezone.utc).isoformat(), "level": level, "message": str(message)[:1000]}
    def update(task):
        task.logs = (task.logs or [])[-99:] + [entry]
    _mutate(task_id, update)


def _progress(task_id: str, value: int) -> None:
    _mutate(task_id, lambda task: setattr(task, "progress", max(0, min(100, int(value)))))


def _checkpoint(task_id: str) -> bool:
    """Return False when cancelled; wait cooperatively while pause is requested."""
    while True:
        db = SessionLocal()
        try:
            task = db.get(AgentTask, task_id)
            if task is None or task.cancel_requested:
                if task is not None:
                    task.status = "cancelled"
                    task.finished_at = datetime.now(timezone.utc)
                    task.progress = min(task.progress, 99)
                    db.commit()
                return False
            if not task.pause_requested:
                if task.status == "paused":
                    task.status = "running"
                    db.commit()
                return True
            if task.status != "paused":
                task.status = "paused"
                db.commit()
        finally:
            db.close()
        time.sleep(1)


def _read_json_report() -> dict[str, Any]:
    path = Path(os.environ.get(
        "AGENTIC_MONITOR_REPORT_PATH",
        "/run/advanced-rag-monitoring/agent_report.json",
    )).expanduser()
    if not path.is_file():
        raise RuntimeError(
            f"Monitoring report not found at {path}. Run monitoring/monitor_server.py "
            "on the host and mount/provide its report to the API container."
        )
    if path.stat().st_size > 5 * 1024 * 1024:
        raise RuntimeError("Monitoring report exceeds the 5 MiB safety limit.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Monitoring report must be a JSON object.")
    return data


def _api_monitor(task_id: str) -> dict[str, Any]:
    _log(task_id, "Checking the configured internal API health endpoint.")
    url = os.environ.get("AGENTIC_SELF_HEALTH_URL", "http://127.0.0.1:8000/health")
    request = urllib.request.Request(url, headers={"User-Agent": "AdvancedRAG-AgentMonitor/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read(16_384).decode("utf-8", errors="replace")
            ok = 200 <= response.status < 300
            result = {"url": url, "ok": ok, "http_status": response.status, "body_excerpt": body[:500]}
            _log(task_id, f"Health probe returned HTTP {response.status}.", "info" if ok else "warning")
            return result
    except Exception as exc:
        _log(task_id, f"Health probe failed: {type(exc).__name__}: {exc}", "error")
        return {"url": url, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}


def _log_analysis(task_id: str) -> dict[str, Any]:
    _log(task_id, "Reading the existing monitoring JSON report; raw container logs are not collected.")
    report = _read_json_report()
    api = report.get("api_health") or {}
    docker = report.get("docker") or {}
    containers = docker.get("containers") or []
    log_summary = report.get("log_summary") or []
    warnings = report.get("warnings") or []
    findings = []
    if not api.get("ok", False):
        findings.append("API health check is not OK in the monitoring snapshot.")
    for container in containers:
        if isinstance(container, dict) and str(container.get("state", "")).lower() != "running":
            findings.append(f"Container {container.get('service') or container.get('name') or 'unknown'} is not running.")
        elif isinstance(container, dict) and str(container.get("health", "")).lower() == "unhealthy":
            findings.append(f"Container {container.get('service') or container.get('name') or 'unknown'} is unhealthy.")
    for item in log_summary:
        if isinstance(item, dict) and int(item.get("error_like_line_count") or 0) > 0:
            findings.append(f"{item.get('service', 'unknown')} log scan found {item.get('error_like_line_count')} error-like lines; inspect the host-side logs.")
    findings.extend(str(w)[:500] for w in warnings if w)
    findings = list(dict.fromkeys(findings))
    _log(task_id, f"Monitoring snapshot analyzed; {len(findings)} finding(s) identified.")
    return {
        "generated_at_utc": report.get("generated_at_utc"),
        "api_health": {"ok": bool(api.get("ok", False)), "http_status": api.get("http_status")},
        "container_count": len(containers),
        "log_summary": [
            {"service": x.get("service"), "lines_scanned": x.get("lines_scanned"),
             "error_like_line_count": x.get("error_like_line_count"), "command_ok": x.get("command_ok")}
            for x in log_summary if isinstance(x, dict)
        ],
        "findings": findings,
        "note": "Snapshot analysis only; log-match phrases are indicators, not proof of an active incident. No corrective actions were taken.",
    }


def _langfuse_report(task_id: str) -> dict[str, Any]:
    _log(task_id, "Fetching available Langfuse observations.")
    from langfuse import get_client
    client = get_client()
    response = client.api.observations.get_many(limit=100, fields="core,basic,time,metadata")
    observations = getattr(response, "data", None)
    if observations is None and isinstance(response, dict):
        observations = response.get("data", [])
    observations = observations or []
    safe = []
    errors = 0
    for obs in observations:
        def val(name, default=None):
            return obs.get(name, default) if isinstance(obs, dict) else getattr(obs, name, default)
        metadata = val("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        retrieval_error = metadata.get("retrieval_error")
        level = str(val("level", "") or "").lower()
        if retrieval_error or level in {"error", "fatal"}:
            errors += 1
        safe.append({
            "id": val("id"), "trace_id": val("trace_id"), "name": val("name"),
            "type": val("type"), "level": val("level"),
            "start_time": str(val("start_time")) if val("start_time") else None,
            "duration_ms": val("duration_ms"),
            "metadata": {
                key: metadata.get(key) for key in
                ("retrieval_count", "retrieval_error", "evidence_found", "attempts", "timings_ms")
                if key in metadata
            },
        })
    _log(task_id, f"Langfuse returned {len(safe)} observation(s), with {errors} error-like observation(s).")
    return {"observation_count": len(safe), "error_like_count": errors, "observations": safe,
            "note": "Only available observations and whitelisted metadata are included."}



def _send_report_email(agent_type: str, result: dict[str, Any]) -> dict[str, Any]:
    """Send a generated report through Gmail API only when explicitly enabled."""
    if os.getenv("AGENT_REPORT_EMAIL_ENABLED", "false").strip().lower() not in {"1", "true", "yes"}:
        return {"status": "skipped", "reason": "Email delivery is disabled by configuration."}
    recipient = os.getenv("AGENT_REPORT_EMAIL_TO", "").strip()
    token_path = Path(os.getenv("GMAIL_TOKEN_PATH", "/run/secrets/gmail-token.json"))
    if not recipient or "@" not in recipient:
        return {"status": "failed", "error": "AGENT_REPORT_EMAIL_TO is not configured with a valid recipient."}
    if not token_path.is_file():
        return {"status": "failed", "error": "Gmail OAuth token file is unavailable to the API container."}
    try:
        import base64
        from email.mime.text import MIMEText
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials.from_authorized_user_file(
            str(token_path), scopes=["https://www.googleapis.com/auth/gmail.send"]
        )
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # Do not write refreshed credentials back into a read-only secret mount.
        if not creds.valid:
            return {"status": "failed", "error": "Gmail OAuth credentials are invalid or expired; re-authorize Gmail."}
        body = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        if len(body) > 18000:
            body = body[:18000] + "\n\n[Report truncated in email. Open the Admin Dashboard to download the complete report.]"
        message = MIMEText(
            "Advanced RAG Agentic AI report\n"
            f"Report type: {agent_type}\n"
            f"Generated UTC: {datetime.now(timezone.utc).isoformat()}\n\n"
            f"{body}",
            "plain",
            "utf-8",
        )
        message["to"] = recipient
        message["subject"] = f"Advanced RAG Agentic AI - {agent_type.replace('_', ' ').title()}"
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        response = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"status": "sent", "message_id": response.get("id"), "recipient": recipient}
    except Exception as exc:
        # Avoid logging request payloads or credential contents.
        return {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}


def _run_one(task_id: str, agent_type: str) -> dict[str, Any]:
    if not _checkpoint(task_id):
        raise InterruptedError("Task was cancelled.")
    if agent_type == "api_monitor":
        return _api_monitor(task_id)
    if agent_type == "log_analysis":
        return _log_analysis(task_id)
    if agent_type == "langfuse_report":
        return _langfuse_report(task_id)
    raise ValueError(f"Unsupported agent type: {agent_type}")


def _worker(task_id: str) -> None:
    db = SessionLocal()
    try:
        task = db.get(AgentTask, task_id)
        if task is None or task.status in TERMINAL:
            with _registry_lock:
                _workers.pop(task_id, None)
            return
        task.status = "running"
        task.started_at = task.started_at or datetime.now(timezone.utc)
        db.commit()
        agent_type = task.agent_type
    finally:
        db.close()

    try:
        if agent_type == "daily_report":
            results = {}
            agents = ["api_monitor", "log_analysis", "langfuse_report"]
            for idx, name in enumerate(agents):
                if not _checkpoint(task_id):
                    return
                _log(task_id, f"Running daily report stage: {name}.")
                try:
                    results[name] = _run_one(task_id, name)
                except InterruptedError:
                    return
                except Exception as exc:
                    results[name] = {"error": f"{type(exc).__name__}: {str(exc)[:500]}"}
                    _log(task_id, f"{name} stage failed: {type(exc).__name__}: {exc}", "error")
                _progress(task_id, int((idx + 1) * 90 / len(agents)))
            result = {"generated_at_utc": datetime.now(timezone.utc).isoformat(),
                      "report_type": "daily_monitoring_summary", "stages": results,
                      "note": "Read-only summary. Review findings before taking any corrective action."}
        else:
            result = _run_one(task_id, agent_type)
        if not _checkpoint(task_id):
            return
        stage_failures = []
        if agent_type == "daily_report":
            stage_failures = [
                name for name, stage in result.get("stages", {}).items()
                if isinstance(stage, dict) and "error" in stage
            ]

        if agent_type in {"langfuse_report", "daily_report"}:
            if stage_failures:
                result["email_delivery"] = {"status": "skipped", "reason": "Report has failed stages; review it in the dashboard before sending."}
                _log(task_id, "Email delivery skipped because one or more report stages failed.", "warning")
            else:
                delivery = _send_report_email(agent_type, result)
                result["email_delivery"] = delivery
                if delivery.get("status") == "sent":
                    _log(task_id, "Report email accepted by Gmail API.")
                elif delivery.get("status") == "failed":
                    _log(task_id, f"Report email failed: {delivery.get('error', 'unknown error')}", "error")
                else:
                    _log(task_id, "Report email not sent: " + str(delivery.get("reason", "disabled")), "warning")

        def complete(task):
            task.result = result
            task.progress = 100 if not stage_failures else min(task.progress, 99)
            task.status = "failed" if stage_failures else "completed"
            task.finished_at = datetime.now(timezone.utc)
            task.error_summary = (
                "Daily report failed stages: " + ", ".join(stage_failures)
                if stage_failures else None
            )

        _mutate(task_id, complete)
        if stage_failures:
            _log(task_id, "Daily report incomplete; failed stages: " + ", ".join(stage_failures), "error")
        else:
            _log(task_id, "Task completed.")
    except InterruptedError:
        _log(task_id, "Task cancelled cooperatively.", "warning")
    except Exception as exc:
        _log(task_id, f"Task failed: {type(exc).__name__}: {exc}", "error")
        def fail(task):
            task.status = "failed"
            task.error_summary = f"{type(exc).__name__}: {str(exc)[:1000]}"
            task.finished_at = datetime.now(timezone.utc)
        _mutate(task_id, fail)
    finally:
        with _registry_lock:
            _workers.pop(task_id, None)


def _start_worker(task_id: str) -> None:
    with _registry_lock:
        existing = _workers.get(task_id)
        if existing and existing.is_alive():
            return
        thread = threading.Thread(target=_worker, args=(task_id,), name=f"agent-task-{task_id[:8]}", daemon=True)
        _workers[task_id] = thread
        thread.start()


@router.get("")
def list_tasks(admin=Depends(admin_user), db=Depends(get_db)):
    tasks = db.scalars(select(AgentTask).order_by(AgentTask.created_at.desc()).limit(100)).all()
    return {"tasks": [_task_dict(task) for task in tasks],
            "execution_mode": "in_process_single_api_worker",
            "schedule_enabled": False}


@router.get("/reports")
def list_reports(admin=Depends(admin_user), db=Depends(get_db)):
    """Return persisted report runs; report data is already stored in AgentTask.result."""
    tasks = db.scalars(
        select(AgentTask)
        .where(AgentTask.agent_type.in_(["langfuse_report", "daily_report"]))
        .order_by(AgentTask.created_at.desc())
        .limit(100)
    ).all()
    return {"reports": [_task_dict(task) for task in tasks]}


@router.get("/reports/{task_id}/download")
def download_report(task_id: str, admin=Depends(admin_user), db=Depends(get_db)):
    task = db.get(AgentTask, task_id)
    if task is None or task.agent_type not in {"langfuse_report", "daily_report"}:
        raise HTTPException(status_code=404, detail="Report not found.")
    if not task.result:
        raise HTTPException(status_code=409, detail="This task does not have a report result yet.")
    payload = {
        "id": task.id, "agent_type": task.agent_type, "status": task.status,
        "created_at": _iso(task.created_at), "finished_at": _iso(task.finished_at),
        "error_summary": task.error_summary, "report": task.result,
    }
    _audit(db, admin, "agent_report.downloaded", task)
    db.commit()
    filename = f"advanced-rag-{task.agent_type}-{task.id}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("")
def create_task(body: TaskCreate, admin=Depends(admin_user), db=Depends(get_db)):
    task = AgentTask(agent_type=body.agent_type, status="pending", progress=0,
                     requested_by_user_id=admin.id, params=body.params or {}, logs=[])
    db.add(task)
    db.flush()
    _audit(db, admin, "agent_task.created", task)
    db.commit()
    db.refresh(task)
    _start_worker(task.id)
    return _task_dict(task)


@router.post("/{task_id}/{action}")
def task_action(task_id: str, action: Literal["pause", "resume", "cancel"], admin=Depends(admin_user), db=Depends(get_db)):
    task = db.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Agent task not found.")
    if action == "pause":
        if task.status != "running":
            raise HTTPException(status_code=409, detail="Only a running task can be paused.")
        task.pause_requested = True
    elif action == "resume":
        if task.status not in {"paused", "running"}:
            raise HTTPException(status_code=409, detail="Only a paused task can be resumed.")
        task.pause_requested = False
        task.status = "running"
        with _registry_lock:
            worker = _workers.get(task.id)
            worker_alive = worker is not None and worker.is_alive()
        if not worker_alive:
            db.commit()
            _start_worker(task.id)
    else:
        if task.status in TERMINAL:
            raise HTTPException(status_code=409, detail="Task is already finished.")
        task.cancel_requested = True
    _audit(db, admin, f"agent_task.{action}_requested", task)
    db.commit()
    db.refresh(task)
    return _task_dict(task)


@router.delete("/{task_id}")
def delete_task(task_id: str, admin=Depends(admin_user), db=Depends(get_db)):
    task = db.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Agent task not found.")
    if task.status not in TERMINAL:
        raise HTTPException(status_code=409, detail="Cancel the task and wait for a terminal status before deleting it.")
    _audit(db, admin, "agent_task.deleted", task)
    db.delete(task)
    db.commit()
    return {"ok": True, "task_id": task_id}


# ---------------------------------------------------------------------------
# Consolidated monitoring reporting API (read-only reporting database)
# ---------------------------------------------------------------------------
from sqlalchemy import create_engine, text
from fastapi.responses import FileResponse

_REPORTING_ENGINE = None
_REPORTING_ENGINE_LOCK = threading.Lock()
_REPORTS_ROOT = Path("/var/lib/advanced-rag-reports").resolve()


def _reporting_engine():
    global _REPORTING_ENGINE
    database_url = os.getenv("REPORTING_DATABASE_URL")
    if not database_url:
        raise HTTPException(
            status_code=503,
            detail="Reporting database is not configured.",
        )
    with _REPORTING_ENGINE_LOCK:
        if _REPORTING_ENGINE is None:
            _REPORTING_ENGINE = create_engine(
                database_url,
                pool_pre_ping=True,
                pool_recycle=300,
                connect_args={"options": "-c default_transaction_read_only=on"},
            )
    return _REPORTING_ENGINE


def _reporting_rows(statement: str, params: dict | None = None):
    try:
        with _reporting_engine().connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    text(statement), params or {}
                ).mappings().all()
            ]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Reporting database is temporarily unavailable.",
        )


def _reporting_audit(db, admin, action: str, details: dict):
    db.add(AuditEvent(
        actor_user_id=admin.id,
        action=action,
        details=details,
    ))
    db.commit()


@router.get("/reporting/cycles")
def reporting_cycles(
    limit: int = 100,
    admin=Depends(admin_user),
    db=Depends(get_db),
):
    limit = max(1, min(limit, 500))
    rows = _reporting_rows(
        """
        SELECT id, cycle_key, window_start, window_end, status,
               expected_agents, validation, retry_count, created_at,
               finished_at
        FROM monitoring_reporting_cycles
        ORDER BY window_start DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    for cycle in rows:
        cycle["agents"] = _reporting_rows(
            """
            SELECT agent_type, task_id, status, error_summary,
                   attempts, started_at, finished_at
            FROM monitoring_reporting_cycle_agents
            WHERE cycle_id = :cycle_id
            ORDER BY agent_type
            """,
            {"cycle_id": cycle["id"]},
        )
    _reporting_audit(
        db, admin, "monitoring_reporting.cycles_viewed",
        {"count": len(rows)},
    )
    return {"cycles": rows, "count": len(rows)}


@router.get("/reporting/reports")
def reporting_reports(
    limit: int = 100,
    admin=Depends(admin_user),
    db=Depends(get_db),
):
    limit = max(1, min(limit, 500))
    rows = _reporting_rows(
        """
        SELECT id, report_date, status, validation, html_path,
               excel_path, json_path, created_at, generated_at
        FROM monitoring_daily_reports
        ORDER BY report_date DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    _reporting_audit(
        db, admin, "monitoring_reporting.reports_viewed",
        {"count": len(rows)},
    )
    return {"reports": rows, "count": len(rows)}


@router.get("/reporting/reports/{report_date}/download/{file_format}")
def download_daily_reporting_file(
    report_date: str,
    file_format: Literal["html", "xlsx", "json"],
    admin=Depends(admin_user),
    db=Depends(get_db),
):
    try:
        parsed_date = datetime.strptime(report_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must use YYYY-MM-DD.")

    rows = _reporting_rows(
        """
        SELECT html_path, excel_path, json_path
        FROM monitoring_daily_reports
        WHERE report_date = :report_date
        LIMIT 1
        """,
        {"report_date": parsed_date},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Daily report not found.")

    path_value = rows[0][{
        "html": "html_path",
        "xlsx": "excel_path",
        "json": "json_path",
    }[file_format]]

    if not path_value:
        raise HTTPException(status_code=404, detail="Report file is unavailable.")

    candidate = Path(path_value).resolve()
    if not candidate.is_relative_to(_REPORTS_ROOT):
        raise HTTPException(status_code=403, detail="Invalid report file path.")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Report file is missing.")

    _reporting_audit(
        db, admin, "monitoring_reporting.report_downloaded",
        {"report_date": report_date, "format": file_format},
    )
    media_types = {
        "html": "text/html",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "json": "application/json",
    }
    return FileResponse(
        path=str(candidate),
        media_type=media_types[file_format],
        filename=f"advanced-rag-daily-report-{report_date}.{file_format}",
    )


@router.get("/reporting/email/{report_date}")
def reporting_email_status(
    report_date: str,
    admin=Depends(admin_user),
    db=Depends(get_db),
):
    try:
        parsed_date = datetime.strptime(report_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must use YYYY-MM-DD.")

    rows = _reporting_rows(
        """
        SELECT report_date, recipient, status, attempts, message_id,
               last_error, created_at, sent_at
        FROM monitoring_email_deliveries
        WHERE report_date = :report_date
        ORDER BY created_at DESC
        """,
        {"report_date": parsed_date},
    )
    _reporting_audit(
        db, admin, "monitoring_reporting.email_status_viewed",
        {"report_date": report_date},
    )
    return {"report_date": report_date, "deliveries": rows}
