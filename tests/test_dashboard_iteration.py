"""Tests for the /iterations/<hash> permanent-record page (R6 read-only).

Covers the 7 design-review states + XSS guards. Rollback form wiring is
tested separately in test_dashboard_rollback.py once that ships.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from satdeploy.dashboard import git_utils
from satdeploy.dashboard.app import create_app
from satdeploy.history import DeploymentRecord, History


@pytest.fixture(autouse=True)
def clear_git_cache():
    git_utils.clear_cache()
    yield
    git_utils.clear_cache()


@pytest.fixture
def make_app(tmp_path: Path):
    """Factory fixture — call with a seeding function to get (client, db)."""
    def _make(seed=None):
        db = tmp_path / "h.db"
        History(db).init_db()
        if seed:
            seed(History(db))
        return TestClient(create_app(db, "testsecret")), db
    return _make


def _push(h: History, app_name="controller", module="som1",
          file_hash="aaaa1111bbbb2222", git_hash="deadbeefcafe1234",
          timestamp="2026-04-20T09:00:00", success=True,
          error_message=None, source="cli"):
    h.record(DeploymentRecord(
        app=app_name, module=module, file_hash=file_hash,
        remote_path=f"/opt/{app_name}", action="push", success=success,
        timestamp=timestamp, git_hash=git_hash,
        error_message=error_message, source=source,
    ))


def test_iteration_404_for_unknown_hash(make_app):
    client, _ = make_app()
    resp = client.get("/iterations/0000000000000000")
    assert resp.status_code == 404
    assert "No iteration found" in resp.text
    assert "0000000000000000" in resp.text


def test_iteration_happy_path_renders_all_fields(make_app):
    client, _ = make_app(lambda h: _push(h))
    with patch.object(git_utils, "_run_git_show", return_value="diff --git a/foo b/foo\n+new line"):
        resp = client.get("/iterations/aaaa1111bbbb2222")
    assert resp.status_code == 200
    body = resp.text
    assert "controller" in body
    assert "som1" in body
    assert "aaaa1111bbbb" in body
    assert "deadbeefcafe" in body
    assert "2026-04-20T09:00:00" in body
    assert "diff --git" in body
    assert "deployed" in body
    # Happy path = this iteration is still the live version, so no rollback button.
    assert "already live" in body
    assert "rollback-trigger" not in body


def test_iteration_no_git_hash_shows_placeholder(make_app):
    client, _ = make_app(lambda h: _push(h, git_hash=None))
    resp = client.get("/iterations/aaaa1111bbbb2222")
    assert resp.status_code == 200
    body = resp.text
    assert "No git provenance" in body
    # Shouldn't try to run git and fall into the "not local" branch.
    assert "not in the local git" not in body


def test_iteration_commit_not_local_shows_copy_button(make_app):
    client, _ = make_app(lambda h: _push(h, git_hash="99999999999999"))
    with patch.object(git_utils, "_run_git_show",
                      side_effect=git_utils.GitLookupError("bad object")):
        resp = client.get("/iterations/aaaa1111bbbb2222")
    assert resp.status_code == 200
    body = resp.text
    assert "isn't in the local git" in body
    assert "Copy full hash" in body
    # Full hash present in the data-copy attribute, not just the truncated form.
    assert 'data-copy="99999999999999"' in body


def test_iteration_rolled_back_shows_yellow_banner(make_app):
    def seed(h: History):
        _push(h, file_hash="oldhash1111", timestamp="2026-04-01T09:00:00")
        # Later deploy supersedes the first one on the same module+app.
        _push(h, file_hash="newhash2222", timestamp="2026-04-10T09:00:00")
    client, _ = make_app(seed)
    resp = client.get("/iterations/oldhash1111")
    body = resp.text
    assert "no longer live" in body
    assert 'href="/iterations/newhash2222"' in body


def test_iteration_rollback_failed_shows_red_banner(make_app):
    def seed(h: History):
        _push(h, file_hash="aaaa", timestamp="2026-04-01T09:00:00")
        h.record(DeploymentRecord(
            app="controller", module="som1", file_hash="aaaa",
            remote_path="/opt/controller", action="rollback", success=False,
            error_message="target unreachable", timestamp="2026-04-20T09:00:00",
        ))
    client, _ = make_app(seed)
    resp = client.get("/iterations/aaaa")
    body = resp.text
    assert "Last rollback attempt failed" in body
    assert "target unreachable" in body


def test_iteration_multiple_events_appear_in_timeline(make_app):
    def seed(h: History):
        _push(h, file_hash="same123", timestamp="2026-04-01T09:00:00")
        h.record(DeploymentRecord(
            app="controller", module="som1", file_hash="same123",
            remote_path="/opt/controller", action="rollback", success=True,
            timestamp="2026-04-10T09:00:00",
        ))
    client, _ = make_app(seed)
    body = client.get("/iterations/same123").text
    # Both events present
    assert body.count("2026-04-01T09:00:00") >= 1
    assert body.count("2026-04-10T09:00:00") >= 1
    # Timeline renders push and rollback classes
    assert "action--push" in body
    assert "action--rollback" in body


def test_iteration_xss_app_name_is_escaped(make_app):
    def seed(h: History):
        _push(h, app_name="<script>alert(1)</script>", file_hash="evil")
    client, _ = make_app(seed)
    body = client.get("/iterations/evil").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_iteration_xss_in_diff_is_escaped(make_app):
    client, _ = make_app(lambda h: _push(h))
    evil_diff = "diff --git a/x b/x\n+<script>alert('pwn')</script>"
    with patch.object(git_utils, "_run_git_show", return_value=evil_diff):
        body = client.get("/iterations/aaaa1111bbbb2222").text
    assert "<script>alert('pwn')</script>" not in body
    assert "&lt;script&gt;alert" in body


def test_iteration_xss_in_error_message_is_escaped(make_app):
    def seed(h: History):
        _push(h, file_hash="aaaa")
        h.record(DeploymentRecord(
            app="controller", module="som1", file_hash="aaaa",
            remote_path="/opt/controller", action="rollback", success=False,
            error_message="<img src=x onerror=alert(1)>",
            timestamp="2026-04-20T09:00:00",
        ))
    client, _ = make_app(seed)
    body = client.get("/iterations/aaaa").text
    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img" in body


def test_git_show_cache_hits_on_second_call(make_app):
    client, _ = make_app(lambda h: _push(h))
    calls = {"n": 0}

    def counting_git_show(git_hash, repo=None):
        calls["n"] += 1
        return f"diff for {git_hash}"

    with patch.object(git_utils, "_run_git_show", side_effect=counting_git_show):
        client.get("/iterations/aaaa1111bbbb2222")
        client.get("/iterations/aaaa1111bbbb2222")
        client.get("/iterations/aaaa1111bbbb2222")
    # All 3 requests resolve the same hash → exactly one subprocess call.
    assert calls["n"] == 1
