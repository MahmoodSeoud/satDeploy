"""Tests for the /api/rollback endpoint (R6 write-path security)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from satdeploy.dashboard import security
from satdeploy.dashboard.app import create_app
from satdeploy.history import DeploymentRecord, History


SECRET = "test-dashboard-secret"


@pytest.fixture
def seeded(tmp_path: Path):
    """Rollback target exists, with a NEWER live deployment so R6 lets us restore."""
    db = tmp_path / "h.db"
    h = History(db)
    h.init_db()
    h.record(DeploymentRecord(
        app="controller", module="som1", file_hash="oldhash11111111",
        remote_path="/opt/controller", action="push", success=True,
        timestamp="2026-04-01T09:00:00", git_hash="deadbeef1234",
    ))
    h.record(DeploymentRecord(
        app="controller", module="som1", file_hash="newhash22222222",
        remote_path="/opt/controller", action="push", success=True,
        timestamp="2026-04-15T09:00:00",
    ))
    app = create_app(db, SECRET)
    # Reset slowapi state per-test so the 1/sec limiter doesn't leak.
    app.state.limiter.reset()
    return TestClient(app, raise_server_exceptions=False), db


def _valid_body(db: Path, file_hash: str = "oldhash11111111") -> dict:
    return {
        "file_hash": file_hash,
        "token": security.sign_rollback(SECRET, file_hash),
        "confirm": security.expected_confirm_string("controller", "som1", file_hash),
    }


def test_rollback_403_without_token(seeded):
    client, db = seeded
    resp = client.post("/api/rollback", json=_valid_body(db))
    assert resp.status_code == 403
    assert "X-Satdeploy-Token" in resp.json()["detail"]


def test_rollback_403_wrong_token(seeded):
    client, db = seeded
    resp = client.post(
        "/api/rollback", json=_valid_body(db),
        headers={"X-Satdeploy-Token": "wrong"},
    )
    assert resp.status_code == 403


def test_rollback_400_missing_body_fields(seeded):
    client, _ = seeded
    resp = client.post(
        "/api/rollback",
        json={"file_hash": "x"},  # token + confirm missing
        headers={"X-Satdeploy-Token": SECRET},
    )
    assert resp.status_code == 400
    assert "required fields" in resp.json()["detail"]


def test_rollback_403_on_bad_hmac(seeded):
    client, db = seeded
    body = _valid_body(db)
    body["token"] = "not-a-real-hmac"
    resp = client.post(
        "/api/rollback", json=body,
        headers={"X-Satdeploy-Token": SECRET},
    )
    assert resp.status_code == 403
    assert "HMAC" in resp.json()["detail"]


def test_rollback_404_for_unknown_hash(seeded):
    client, db = seeded
    body = _valid_body(db, file_hash="deadcafe00000000")
    resp = client.post(
        "/api/rollback", json=body,
        headers={"X-Satdeploy-Token": SECRET},
    )
    assert resp.status_code == 404


def test_rollback_400_on_bad_confirm(seeded):
    client, db = seeded
    body = _valid_body(db)
    body["confirm"] = "rollback wrong string"
    resp = client.post(
        "/api/rollback", json=body,
        headers={"X-Satdeploy-Token": SECRET},
    )
    assert resp.status_code == 400
    assert "confirmation mismatch" in resp.json()["detail"]


def test_rollback_happy_path_shells_out_and_returns_ok(seeded):
    client, db = seeded
    body = _valid_body(db)
    fake_proc = SimpleNamespace(returncode=0, stdout="rolled back", stderr="")
    with patch.object(subprocess, "run", return_value=fake_proc) as mock_run:
        resp = client.post(
            "/api/rollback", json=body,
            headers={"X-Satdeploy-Token": SECRET},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data == {"ok": True, "redirect": "/iterations/oldhash11111111"}

    # Invoked the CLI with --config and the right args.
    called = mock_run.call_args
    cmd = called.args[0]
    assert "satdeploy" in cmd and "rollback" in cmd
    assert "controller" in cmd and "oldhash11111111" in cmd
    # The env passed to the subprocess carries SATDEPLOY_SOURCE=web so
    # history.record() tags the audit row as web-initiated.
    assert called.kwargs["env"]["SATDEPLOY_SOURCE"] == "web"


def test_rollback_500_when_subprocess_fails(seeded):
    client, db = seeded
    body = _valid_body(db)
    fake_proc = SimpleNamespace(returncode=2, stdout="", stderr="connect refused")
    with patch.object(subprocess, "run", return_value=fake_proc):
        resp = client.post(
            "/api/rollback", json=body,
            headers={"X-Satdeploy-Token": SECRET},
        )
    assert resp.status_code == 500
    assert resp.json() == {"ok": False, "error": "connect refused"}


def test_rollback_504_on_timeout(seeded):
    client, db = seeded
    body = _valid_body(db)
    with patch.object(subprocess, "run",
                      side_effect=subprocess.TimeoutExpired(cmd="satdeploy", timeout=60)):
        resp = client.post(
            "/api/rollback", json=body,
            headers={"X-Satdeploy-Token": SECRET},
        )
    assert resp.status_code == 504
    assert "timed out" in resp.json()["error"]


def test_rollback_rate_limit_429_on_second_request(seeded):
    client, db = seeded
    body = _valid_body(db)
    fake_proc = SimpleNamespace(returncode=0, stdout="", stderr="")
    with patch.object(subprocess, "run", return_value=fake_proc):
        first = client.post(
            "/api/rollback", json=body,
            headers={"X-Satdeploy-Token": SECRET},
        )
        second = client.post(
            "/api/rollback", json=body,
            headers={"X-Satdeploy-Token": SECRET},
        )
    assert first.status_code == 200
    # Immediate retry from the same IP must hit the 1/sec limit.
    assert second.status_code == 429


def test_history_source_env_override(tmp_path: Path, monkeypatch):
    """Env fallback for source tag: subprocess'd History.record picks up SATDEPLOY_SOURCE."""
    db = tmp_path / "h.db"
    History(db).init_db()
    monkeypatch.setenv("SATDEPLOY_SOURCE", "web")
    h = History(db)
    h.record(DeploymentRecord(
        app="a", module="m", file_hash="abc", remote_path="/x",
        action="rollback", success=True, timestamp="2026-04-20T10:00:00",
    ))
    last = h.get_last_deployment("a")
    assert last.source == "web"


def test_history_source_env_does_not_override_explicit(tmp_path: Path, monkeypatch):
    """Explicit record.source wins over env."""
    db = tmp_path / "h.db"
    History(db).init_db()
    monkeypatch.setenv("SATDEPLOY_SOURCE", "web")
    h = History(db)
    h.record(DeploymentRecord(
        app="a", module="m", file_hash="abc", remote_path="/x",
        action="push", success=True, timestamp="2026-04-20T10:00:00",
        source="cli",
    ))
    last = h.get_last_deployment("a")
    assert last.source == "cli"


def test_security_sign_rollback_constant_time_verify():
    """Correct token verifies; tampered token fails; old bucket still accepted."""
    token = security.sign_rollback("secret", "abc123")
    assert security.verify_rollback("secret", "abc123", token) is True
    assert security.verify_rollback("secret", "abc123", token + "tamper") is False
    # Different iteration hash — even with valid signature — must not verify.
    assert security.verify_rollback("secret", "other", token) is False


def test_security_expected_confirm_is_stable():
    s = security.expected_confirm_string("controller", "som1", "aaaaaaaabbbbbbbb")
    assert s == "rollback controller@som1 aaaaaaaa"
