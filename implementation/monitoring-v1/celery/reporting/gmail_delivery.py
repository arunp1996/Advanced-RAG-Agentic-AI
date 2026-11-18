"""Controlled Gmail delivery for daily monitoring reports."""

import base64
import os
from email.message import EmailMessage
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from reporting import reporting_store as rs

APPROVED_RECIPIENT = "sdsorgonicforms@gmail.com"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.send"


def _build_service():
    token_path = os.getenv(
        "GMAIL_TOKEN_PATH", "/run/secrets/gmail-token.json"
    )

    if not os.path.isfile(token_path) or not os.access(token_path, os.R_OK):
        raise RuntimeError("Gmail OAuth token is unavailable or unreadable.")

    credentials = Credentials.from_authorized_user_file(
        token_path, scopes=[GMAIL_SCOPE]
    )

    if not credentials.valid:
        if not credentials.refresh_token:
            raise RuntimeError("Gmail OAuth refresh token is unavailable.")
        credentials.refresh(Request())

    return build(
        "gmail",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )


def _build_raw_message(report_date, excel_path, test=False):
    recipient = os.getenv("AGENT_REPORT_EMAIL_TO", "").strip()

    if recipient != APPROVED_RECIPIENT:
        raise ValueError("Configured recipient does not match approved recipient.")

    path = Path(excel_path)

    if path.suffix.lower() != ".xlsx":
        raise ValueError("Only a consolidated Excel workbook may be attached.")

    if not path.is_file():
        raise FileNotFoundError(f"Excel report is missing: {path.name}")

    message = EmailMessage()
    prefix = "[TEST] " if test else ""

    message["To"] = APPROVED_RECIPIENT
    message["Subject"] = (
        f"{prefix}Advanced RAG Agentic AI – Daily Observability Report | "
        f"{report_date.strftime('%d-%b-%Y')}"
    )

    message.set_content(
        "Hi Team,\n\n"
        "Please find attached the consolidated Advanced RAG Agentic AI "
        "Daily Observability Report for "
        f"{report_date.strftime('%d-%b-%Y')}.\n\n"
        "The workbook includes:\n"
        "- Manager Summary\n"
        "- Detailed Report\n"
        "- Read Me\n\n"
        f"Reporting Date: {report_date.strftime('%d-%b-%Y')}\n"
        "Timezone: Asia/Kolkata\n\n"
        "Please review the report and let me know if any additional "
        "information or clarification is required.\n\n"
        "Regards,\n"
        "AI Team\n"
        + ("\nThis is a delivery test.\n" if test else "")
    )

    content = path.read_bytes()

    if len(content) > 20 * 1024 * 1024:
        raise ValueError("Excel attachment exceeds the 20 MB safety limit.")

    message.add_attachment(
        content,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )

    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def send_test_email(report_date, excel_path):
    """Send one Excel-only test without claiming the daily delivery record."""
    service = _build_service()
    raw = _build_raw_message(report_date, excel_path, test=True)

    result = service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()

    return {
        "status": "accepted",
        "message_id": result.get("id"),
        "recipient": APPROVED_RECIPIENT,
        "attachment": Path(excel_path).name,
    }


def deliver_daily_report(report_date, excel_path):
    """Send one consolidated Excel report with duplicate protection."""
    service = _build_service()
    raw = _build_raw_message(report_date, excel_path)

    claim = rs.claim_email_delivery(
        report_date,
        APPROVED_RECIPIENT,
        max_attempts=3,
    )

    if not claim:
        existing = rs.get_email_delivery(report_date)
        return {
            "status": (
                existing.get("status", "not_claimed")
                if existing else "not_claimed"
            ),
            "message": "Delivery was not claimed; duplicate send prevented.",
        }

    try:
        result = service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()

    except HttpError as exc:
        status_code = getattr(exc.resp, "status", None)

        if status_code is not None and 400 <= status_code < 500:
            rs.mark_email_delivery_failed(
                report_date,
                f"Gmail rejected the request with HTTP {status_code}.",
            )
            return {"status": "failed", "http_status": status_code}

        rs.mark_email_delivery_uncertain(
            report_date,
            f"Ambiguous Gmail API response: HTTP {status_code or 'unknown'}.",
        )
        return {"status": "uncertain"}

    except Exception as exc:
        rs.mark_email_delivery_uncertain(
            report_date,
            f"Ambiguous Gmail send outcome: {type(exc).__name__}.",
        )
        return {"status": "uncertain"}

    message_id = result.get("id")

    if not rs.mark_email_delivery_sent(report_date, message_id):
        rs.mark_email_delivery_uncertain(
            report_date,
            "Gmail accepted the message but database confirmation failed.",
        )
        return {"status": "uncertain"}

    return {
        "status": "sent",
        "message_id": message_id,
        "recipient": APPROVED_RECIPIENT,
        "attachment": Path(excel_path).name,
    }
