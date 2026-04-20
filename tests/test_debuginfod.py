"""Unit tests for satdeploy.debuginfod (P0 landmine #2: TOCTOU + flock)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from satdeploy import debuginfod


@pytest.fixture(autouse=True)
def redirect_state(tmp_path, monkeypatch):
    """Point all debuginfod state paths at a per-test tmpdir.

    This is run for every test in this module so we never touch the user's
    real ``~/.satdeploy``.
    """
    run_dir = tmp_path / "run"
    pid_file = run_dir / "debuginfod.pid"
    lock_file = run_dir / "debuginfod.pid.lock"
    db_file = run_dir / "debuginfod.sqlite"
    sysroots = tmp_path / "sysroots"
    monkeypatch.setattr(debuginfod, "SATDEPLOY_HOME", tmp_path)
    monkeypatch.setattr(debuginfod, "RUN_DIR", run_dir)
    monkeypatch.setattr(debuginfod, "PID_FILE", pid_file)
    monkeypatch.setattr(debuginfod, "LOCK_FILE", lock_file)
    monkeypatch.setattr(debuginfod, "DB_FILE", db_file)
    monkeypatch.setattr(debuginfod, "DEFAULT_SYSROOTS_DIR", sysroots)
    yield tmp_path


def test_status_returns_none_when_no_pid_file():
    assert debuginfod.status() is None


def test_status_cleans_stale_pid(monkeypatch):
    debuginfod.RUN_DIR.mkdir(parents=True, exist_ok=True)
    debuginfod.PID_FILE.write_text("99999")
    monkeypatch.setattr(debuginfod, "_is_debuginfod_running", lambda _pid: False)
    assert debuginfod.status() is None
    assert not debuginfod.PID_FILE.exists()


def test_status_reports_live_pid(monkeypatch):
    debuginfod.RUN_DIR.mkdir(parents=True, exist_ok=True)
    debuginfod.PID_FILE.write_text("4242")
    monkeypatch.setattr(debuginfod, "_is_debuginfod_running", lambda _pid: True)
    assert debuginfod.status() == 4242


def test_serve_missing_binary_raises(monkeypatch):
    monkeypatch.setattr(debuginfod, "_which_debuginfod", lambda: None)
    with pytest.raises(debuginfod.DebuginfodError, match="debuginfod not found"):
        debuginfod.serve()


def test_serve_reuses_running_server(monkeypatch):
    debuginfod.RUN_DIR.mkdir(parents=True, exist_ok=True)
    debuginfod.PID_FILE.write_text("1234")

    monkeypatch.setattr(debuginfod, "_which_debuginfod", lambda: "/usr/bin/debuginfod")
    monkeypatch.setattr(debuginfod, "_is_debuginfod_running", lambda pid: pid == 1234)

    spawned = MagicMock()
    monkeypatch.setattr(debuginfod.subprocess, "Popen", spawned)

    pid = debuginfod.serve()
    assert pid == 1234
    spawned.assert_not_called()


def test_serve_spawns_when_no_pid(monkeypatch):
    monkeypatch.setattr(debuginfod, "_which_debuginfod", lambda: "/usr/bin/debuginfod")
    monkeypatch.setattr(debuginfod, "_is_debuginfod_running", lambda _pid: False)

    fake_proc = MagicMock()
    fake_proc.pid = 9876

    def fake_popen(cmd, **kwargs):
        assert cmd[0] == "/usr/bin/debuginfod"
        assert "-p" in cmd and str(debuginfod.DEBUGINFOD_PORT) in cmd
        assert kwargs["start_new_session"] is True
        return fake_proc

    monkeypatch.setattr(debuginfod.subprocess, "Popen", fake_popen)

    pid = debuginfod.serve()
    assert pid == 9876
    assert debuginfod.PID_FILE.read_text() == "9876"


def test_serve_replaces_stale_pid(monkeypatch):
    """Stale PID (dead process) → cleaned up and new server spawned."""
    debuginfod.RUN_DIR.mkdir(parents=True, exist_ok=True)
    debuginfod.PID_FILE.write_text("11111")

    monkeypatch.setattr(debuginfod, "_which_debuginfod", lambda: "/usr/bin/debuginfod")
    monkeypatch.setattr(debuginfod, "_is_debuginfod_running", lambda _pid: False)

    fake_proc = MagicMock()
    fake_proc.pid = 22222
    monkeypatch.setattr(debuginfod.subprocess, "Popen", lambda *a, **k: fake_proc)

    pid = debuginfod.serve()
    assert pid == 22222
    assert debuginfod.PID_FILE.read_text() == "22222"


def test_stop_noop_when_not_running():
    assert debuginfod.stop() is False


def test_stop_kills_and_cleans(monkeypatch):
    debuginfod.RUN_DIR.mkdir(parents=True, exist_ok=True)
    debuginfod.PID_FILE.write_text("55555")

    running_state = {"alive": True}
    monkeypatch.setattr(
        debuginfod, "_is_debuginfod_running", lambda pid: running_state["alive"]
    )

    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        running_state["alive"] = False

    monkeypatch.setattr(debuginfod.os, "kill", fake_kill)
    monkeypatch.setattr(debuginfod, "_wait_for_exit", lambda _pid, timeout=5.0: True)

    assert debuginfod.stop() is True
    assert killed[0][0] == 55555
    assert not debuginfod.PID_FILE.exists()


def test_stop_escalates_to_sigkill(monkeypatch):
    """SIGTERM grace expiring → SIGKILL sent."""
    import signal as signal_module

    debuginfod.RUN_DIR.mkdir(parents=True, exist_ok=True)
    debuginfod.PID_FILE.write_text("66666")

    monkeypatch.setattr(debuginfod, "_is_debuginfod_running", lambda _pid: True)

    signals_sent: list[int] = []

    def fake_kill(pid, sig):
        signals_sent.append(sig)

    monkeypatch.setattr(debuginfod.os, "kill", fake_kill)

    wait_calls = {"n": 0}

    def fake_wait(_pid, timeout=5.0):
        wait_calls["n"] += 1
        return wait_calls["n"] >= 2  # first wait times out, second succeeds

    monkeypatch.setattr(debuginfod, "_wait_for_exit", fake_wait)

    assert debuginfod.stop() is True
    assert signal_module.SIGTERM in signals_sent
    assert signal_module.SIGKILL in signals_sent


def test_concurrent_serve_only_spawns_once(monkeypatch):
    """Two serve() calls race — flock ensures exactly one spawn."""
    monkeypatch.setattr(debuginfod, "_which_debuginfod", lambda: "/usr/bin/debuginfod")

    # First call: no PID; second call: PID from first spawn is "running".
    spawn_count = {"n": 0}
    spawned_pids: list[int] = []

    def fake_popen(*_args, **_kwargs):
        spawn_count["n"] += 1
        proc = MagicMock()
        proc.pid = 10000 + spawn_count["n"]
        spawned_pids.append(proc.pid)
        return proc

    monkeypatch.setattr(debuginfod.subprocess, "Popen", fake_popen)

    # After first spawn, /proc lookup says "yes, that pid is debuginfod".
    def is_running(pid):
        return pid in spawned_pids

    monkeypatch.setattr(debuginfod, "_is_debuginfod_running", is_running)

    pid1 = debuginfod.serve()
    pid2 = debuginfod.serve()
    assert pid1 == pid2
    assert spawn_count["n"] == 1
