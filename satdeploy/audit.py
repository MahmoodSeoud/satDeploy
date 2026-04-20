"""Export history.db as a markdown deployment audit report.

Produces a compliance-legible record of every deploy / rollback on the
ground station: one section per app, rows in reverse-chronological order,
with hash, result, transport, and git provenance. The shape is intentionally
boring (markdown table) so it pastes into a mission-review ticket or PDF
without further tooling.

Only reads from ``history.db`` (open with ``mode=ro``). Never writes. Safe
to run while the APM or Python CLI is actively deploying.

Markdown injection is handled in one place: ``_md_cell`` escapes the pipe
and newline characters that break table rendering. The rest of the
project's data is either hashes (hex-only) or ISO timestamps, so most cells
don't need escaping at all, but the helper covers free-text fields
(app name, target, error_message) defensively.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional


def _md_cell(value: Optional[str]) -> str:
    """Escape a string for a markdown table cell. ``None`` becomes ``—``."""
    if value is None:
        return "—"
    return value.replace("|", "\\|").replace("\r", "").replace("\n", " ")


def _short_hash(value: Optional[str], length: int = 8) -> str:
    if not value:
        return "—"
    return value[:length]


def _parse_since(raw: str) -> datetime:
    """Parse an ISO-ish date. Accepts ``YYYY-MM-DD`` and ``YYYY-MM-DDTHH:MM[:SS]``."""
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"Invalid --since value {raw!r}. Use ISO format: "
            f"YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS."
        ) from exc


def export_markdown(
    db_path: Path,
    *,
    since: Optional[datetime] = None,
    app_filter: Optional[str] = None,
    target_filter: Optional[str] = None,
    now: Optional[datetime] = None,
) -> str:
    """Return a markdown deployment report for ``db_path``.

    ``since``, ``app_filter``, ``target_filter`` are optional AND filters.
    ``now`` is injected for deterministic test output.
    """
    generated_at = (now or datetime.now()).isoformat(timespec="seconds")

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")

    query_parts = ["SELECT * FROM deployments WHERE 1=1"]
    params: list[str] = []
    if since is not None:
        query_parts.append("AND timestamp >= ?")
        params.append(since.isoformat())
    if app_filter is not None:
        query_parts.append("AND app = ?")
        params.append(app_filter)
    if target_filter is not None:
        query_parts.append("AND module = ?")
        params.append(target_filter)
    query_parts.append("ORDER BY app, timestamp DESC")

    cursor = conn.execute(" ".join(query_parts), params)
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return _render(rows, generated_at=generated_at,
                   since=since, app_filter=app_filter,
                   target_filter=target_filter)


def _render(
    rows: list[dict],
    *,
    generated_at: str,
    since: Optional[datetime],
    app_filter: Optional[str],
    target_filter: Optional[str],
) -> str:
    out: list[str] = []
    out.append("# satdeploy deployment audit")
    out.append("")
    out.append(f"Generated: {generated_at}")

    filters: list[str] = []
    if since is not None:
        filters.append(f"since={since.isoformat()}")
    if app_filter is not None:
        filters.append(f"app={app_filter}")
    if target_filter is not None:
        filters.append(f"target={target_filter}")
    if filters:
        out.append(f"Filters: {', '.join(filters)}")
    out.append("")

    if not rows:
        out.append("_No deployments found._")
        out.append("")
        return "\n".join(out)

    out.append(f"Total deployments: {len(rows)}")
    out.append("")

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["app"]].append(row)

    for app in sorted(grouped.keys()):
        out.append(f"## {_md_cell(app)}")
        out.append("")
        out.append(
            "| Timestamp | Target | Action | Hash | Result "
            "| Git | Transport | Provenance |"
        )
        out.append(
            "|---|---|---|---|---|---|---|---|"
        )
        for row in grouped[app]:
            if row["success"]:
                result = "\u2713 success"
            else:
                reason = _md_cell(row["error_message"]) if row["error_message"] else "failed"
                result = f"\u2717 {reason}"
            out.append(
                f"| {_md_cell(row['timestamp'])} "
                f"| {_md_cell(row['module'])} "
                f"| {_md_cell(row['action'])} "
                f"| `{_short_hash(row['file_hash'])}` "
                f"| {result} "
                f"| {_short_hash(row['git_hash'], 12)} "
                f"| {_md_cell(row['transport'])} "
                f"| {_md_cell(row['provenance_source'])} |"
            )
        out.append("")

    return "\n".join(out)
