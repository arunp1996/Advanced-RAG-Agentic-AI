
from contextlib import asynccontextmanager
from pathlib import Path
import json
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters


@asynccontextmanager
async def connect_mcp():
    """Start the local MCP server and establish a client session."""
    app_dir = str(Path(__file__).resolve().parent.parent)

    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server"],
        cwd=app_dir,
        env=os.environ.copy(),
    )

    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def _extract_entries(result):
    """Extract document entries from MCP structured or JSON text content."""
    structured = getattr(result, "structuredContent", None)

    if isinstance(structured, list):
        return structured

    if isinstance(structured, dict):
        entries = structured.get("result")
        if isinstance(entries, list):
            return entries

    for item in getattr(result, "content", []) or []:
        raw_text = getattr(item, "text", None)

        if not raw_text:
            continue

        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError):
            continue

        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            entries = data.get("result")
            if isinstance(entries, list):
                return entries

    raise RuntimeError(
        "MCP returned no recognizable document result list"
    )


def _extract_monitoring_report(result):
    """Extract and validate the monitoring report returned by MCP."""
    structured = getattr(result, "structuredContent", None)

    if isinstance(structured, dict):
        report = structured.get("result", structured)

        if isinstance(report, dict) and report:
            return report

    for item in getattr(result, "content", []) or []:
        raw_text = getattr(item, "text", None)

        if not raw_text:
            continue

        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError):
            continue

        if isinstance(data, dict):
            report = data.get("result", data)

            if isinstance(report, dict) and report:
                return report

    raise RuntimeError(
        "MCP returned no recognizable monitoring report"
    )


def _deduplicate_entries(entries, limit):
    """
    Remove duplicate document chunks while preserving retrieval order.

    Prefer document ID + chunk index as the stable identity.
    If those fields are unavailable, use normalized chunk text.
    """
    unique_entries = []
    seen_keys = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        text = entry.get("text", "")

        if not isinstance(text, str) or not text.strip():
            continue

        metadata = {
            "filename": entry.get("filename", "document"),
            "chunk_index": entry.get("chunk_index"),
            "document_id": entry.get("document_id"),
        }

        document_id = metadata["document_id"]
        chunk_index = metadata["chunk_index"]

        if document_id is not None and chunk_index is not None:
            key = (
                "chunk",
                str(document_id),
                str(chunk_index),
            )
        else:
            normalized_text = " ".join(text.casefold().split())
            key = ("text", normalized_text)

        if key in seen_keys:
            continue

        seen_keys.add(key)

        unique_entries.append(
            {
                "text": text,
                "metadata": metadata,
                "distance": entry.get("distance"),
            }
        )

        if len(unique_entries) >= limit:
            break

    return unique_entries


async def retrieve_via_mcp(
    question: str,
    trusted_user_id: int,
    limit: int = 5,
) -> list[dict]:
    """
    Retrieve deduplicated document chunks for the authenticated user.

    Request additional candidates so duplicate chunks are less likely
    to reduce the final number of unique results.
    """
    if not isinstance(question, str) or not question.strip():
        return []

    if (
        not isinstance(trusted_user_id, int)
        or isinstance(trusted_user_id, bool)
        or trusted_user_id <= 0
    ):
        raise ValueError("Invalid authenticated user identity")

    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("Retrieval limit must be an integer")

    # Keep the final result bounded to 1–5 unique chunks.
    safe_limit = max(1, min(limit, 5))

    # Retrieve extra candidates before deduplication.
    candidate_limit = min(safe_limit * 3, 15)

    async with connect_mcp() as session:
        result = await session.call_tool(
            "search_user_documents",
            {
                "question": question.strip(),
                "trusted_user_id": trusted_user_id,
                "limit": candidate_limit,
            },
        )

    if result.isError:
        raise RuntimeError("MCP document retrieval failed")

    entries = _extract_entries(result)

    return _deduplicate_entries(
        entries=entries,
        limit=safe_limit,
    )


async def get_monitoring_report_via_mcp() -> dict:
    """
    Fetch the latest saved, sanitized monitoring report.

    This reads a snapshot; it does not run new server checks.
    """
    async with connect_mcp() as session:
        result = await session.call_tool(
            "get_server_monitoring_report",
            {},
        )

    if result.isError:
        raise RuntimeError("MCP monitoring report retrieval failed")

    report = _extract_monitoring_report(result)

    if not any(
        key in report
        for key in (
            "generated_at_utc",
            "api",
            "containers",
            "warnings",
        )
    ):
        raise RuntimeError(
            "Monitoring report has an unexpected format"
        )

    return report
