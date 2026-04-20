"""Tests for satdeploy.audit — markdown export + filters + injection safety."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from satdeploy import audit
from satdeploy.history import DeploymentRecord, History


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "history.db"
    history = History(db_path)
    history.init_db()
    history.record(DeploymentRecord(
        app="controller", module="som1", file_hash="aaaa1111", remote_path="/opt/controller",
        action="push", success=True, timestamp="2026-04-01T09:00:00",
        git_hash="deadbeefcafe", provenance_source="local", transport="ssh",
    ))
    history.record(DeploymentRecord(
        app="controller", module="som1", file_hash="bbbb2222", remote_path="/opt/controller",
        action="rollback", success=True, timestamp="2026-04-15T11:30:00",
        git_hash=None, provenance_source="local",
    ))
    history.record(DeploymentRecord(
        app="libparam", module="som2", file_hash="cccc3333", remote_path="/usr/lib/libparam.so",
        action="push", success=False, error_message="service failed to restart",
        timestamp="2026-04-10T14:00:00",
    ))
    return db_path


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 4, 20, 10, 0, 0)


def test_export_happy_path_has_one_section_per_app(seeded_db, fixed_now):
    out = audit.export_markdown(seeded_db, now=fixed_now)
    assert "# satdeploy deployment audit" in out
    assert "Generated: 2026-04-20T10:00:00" in out
    assert "Total deployments: 3" in out
    assert "## controller" in out
    assert "## libparam" in out


def test_export_renders_hash_and_outcome(seeded_db, fixed_now):
    out = audit.export_markdown(seeded_db, now=fixed_now)
    assert "`aaaa1111`" in out
    assert "`bbbb2222`" in out
    assert "✓ success" in out
    assert "✗ service failed to restart" in out


def test_export_empty_db_returns_placeholder(tmp_path, fixed_now):
    db_path = tmp_path / "empty.db"
    History(db_path).init_db()
    out = audit.export_markdown(db_path, now=fixed_now)
    assert "_No deployments found._" in out
    assert "## " not in out  # no app sections


def test_export_filter_by_app(seeded_db, fixed_now):
    out = audit.export_markdown(seeded_db, app_filter="controller", now=fixed_now)
    assert "## controller" in out
    assert "## libparam" not in out
    assert "Filters: app=controller" in out


def test_export_filter_by_target(seeded_db, fixed_now):
    out = audit.export_markdown(seeded_db, target_filter="som2", now=fixed_now)
    assert "## libparam" in out
    assert "## controller" not in out


def test_export_filter_by_since(seeded_db, fixed_now):
    out = audit.export_markdown(
        seeded_db, since=datetime(2026, 4, 14), now=fixed_now
    )
    # Only rows on/after 2026-04-14 should appear: controller rollback (04-15) only
    assert "2026-04-15T11:30:00" in out
    assert "2026-04-01T09:00:00" not in out
    assert "2026-04-10T14:00:00" not in out


def test_markdown_injection_app_name_with_pipes_and_newlines(tmp_path, fixed_now):
    db_path = tmp_path / "h.db"
    history = History(db_path)
    history.init_db()
    history.record(DeploymentRecord(
        app="evil|name\nwith\npipes",
        module="mod",
        file_hash="ff",
        remote_path="/x",
        action="push",
        success=True,
        timestamp="2026-04-20T09:00:00",
    ))
    out = audit.export_markdown(db_path, now=fixed_now)
    # Pipes escaped so table doesn't break
    assert "evil\\|name" in out
    # Newlines collapsed to space
    assert "evil\\|name with pipes" in out
    # No raw pipes in the header line of the fake app
    header_line = [l for l in out.splitlines() if l.startswith("## ")][0]
    assert "\n" not in header_line


def test_markdown_injection_error_message_with_pipe(tmp_path, fixed_now):
    db_path = tmp_path / "h.db"
    history = History(db_path)
    history.init_db()
    history.record(DeploymentRecord(
        app="a", module="m", file_hash="ff", remote_path="/x",
        action="push", success=False,
        error_message="boom | table | break\nmore",
        timestamp="2026-04-20T09:00:00",
    ))
    out = audit.export_markdown(db_path, now=fixed_now)
    row_line = [l for l in out.splitlines() if "boom" in l][0]
    # 8 columns → 9 unescaped pipes (1 leading + 7 separators + 1 trailing).
    # Any extra pipe from the injected message must be escaped to `\|`.
    unescaped = row_line.replace("\\|", "")
    assert unescaped.count("|") == 9, (
        f"Injection broke the table; unescaped-pipe count was "
        f"{unescaped.count('|')}, want 9. Row: {row_line!r}"
    )


def test_parse_since_accepts_date_only():
    assert audit._parse_since("2026-04-01") == datetime(2026, 4, 1)


def test_parse_since_accepts_datetime():
    assert audit._parse_since("2026-04-01T12:30:00") == datetime(2026, 4, 1, 12, 30, 0)


def test_parse_since_rejects_garbage():
    with pytest.raises(ValueError, match="Invalid --since"):
        audit._parse_since("not a date")


def test_md_cell_escapes_pipes_and_newlines():
    assert audit._md_cell("a|b") == "a\\|b"
    assert audit._md_cell("line1\nline2") == "line1 line2"
    assert audit._md_cell(None) == "—"
    assert audit._md_cell("no specials") == "no specials"


def test_short_hash_truncates_and_handles_none():
    assert audit._short_hash("abcdef1234567890") == "abcdef12"
    assert audit._short_hash("abcdef1234567890", length=12) == "abcdef123456"
    assert audit._short_hash(None) == "—"
    assert audit._short_hash("") == "—"
