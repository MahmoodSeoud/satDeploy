"""Tests for satdeploy.iterate — the wedge (edit-to-running).

Keeps the real transport out of the loop via mocks. The CSP / SSH agent
integration lives under `tests/integration/` (Lane A follow-up work); these
unit tests cover iterate.py's composition logic: flock, ABI gate, transport
dispatch, history row shape, debug-path spawn, error routing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from satdeploy import errors, iterate
from satdeploy.config import AppConfig, Config, ModuleConfig
from satdeploy.transport.base import DeployResult, TransportError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_binary(tmp_path):
    """A tiny ELF-ish file standing in for a compiled app binary."""
    bin_path = tmp_path / "controller"
    bin_path.write_bytes(b"\x7fELF" + b"\x00" * 256)
    return bin_path


@pytest.fixture
def sysroot_complete(tmp_path):
    """Sysroot with every lib the ABI check would look for."""
    sr = tmp_path / "sysroot"
    (sr / "usr" / "lib").mkdir(parents=True)
    for lib in ("libparam.so.3", "libcsp.so", "libc.so.6"):
        (sr / "usr" / "lib" / lib).touch()
    return sr


@pytest.fixture
def fake_config(tmp_path, tmp_binary, monkeypatch):
    """A Config stub whose history_path lives under tmp_path (no shared
    state between tests) and exposes one app called 'controller'.

    Matches the real Config's public surface — see satdeploy/config.py:139
    for `config_path` (property). Using ``spec=Config`` ensures tests
    fail at setup if we reference an attribute the real class doesn't
    have, rather than at a non-deterministic call site.
    """
    cfg = MagicMock(spec=Config)
    cfg.config_path = tmp_path / "config.yaml"
    cfg.history_path = tmp_path / "history.db"
    cfg.get_backup_dir = lambda name: str(tmp_path / "backups" / name)
    cfg.get_all_app_names = lambda: ["controller"]
    cfg.get_app = lambda name: (
        AppConfig(
            name="controller",
            local=str(tmp_binary),
            remote="/opt/disco/bin/controller",
            service="controller.service",
        ) if name == "controller" else None
    )
    return cfg


@pytest.fixture
def fake_module(tmp_path):
    """A plain SSH ModuleConfig that iterate knows how to dispatch."""
    return ModuleConfig(
        name="som1",
        transport="ssh",
        host="10.0.0.42",
        user="root",
    )


@pytest.fixture(autouse=True)
def clean_lock_dir(tmp_path, monkeypatch):
    """Redirect the per-app lock dir into tmp_path so tests don't contend
    with each other or with any running dev workflow."""
    monkeypatch.setattr(iterate, "ITERATE_LOCK_DIR", tmp_path / "run")


@pytest.fixture
def mocked_transport():
    """A Transport double that claims success on deploy. Wired in via
    satdeploy.cli.get_transport patch."""
    t = MagicMock()
    t.connect.return_value = None
    t.disconnect.return_value = None
    t.deploy.return_value = DeployResult(
        success=True,
        backup_path="/opt/satdeploy/backups/controller/20260421-abc.bak",
        file_hash="abcdef01",
    )
    return t


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_run_iterate_happy_path_returns_result(
    fake_config, fake_module, mocked_transport, tmp_binary
):
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport), \
         patch("satdeploy.iterate.resolve_provenance",
               return_value=("3f4a2e1", "local")):
        result = iterate.run_iterate(fake_config, fake_module, "controller")

    assert isinstance(result, iterate.IterateResult)
    assert result.app == "controller"
    # Project convention: compute_file_hash returns 8 hex chars (matches
    # the agent's backup naming, dashboard short-hash column, and the
    # transport.deploy expected_checksum contract).
    assert len(result.file_hash) == 8
    assert result.elapsed_s >= 0.0
    assert result.debug_url is None
    mocked_transport.connect.assert_called_once()
    mocked_transport.deploy.assert_called_once()
    mocked_transport.disconnect.assert_called_once()


def test_run_iterate_records_history_row_with_correct_transport(
    fake_config, fake_module, mocked_transport, tmp_path
):
    fake_module.transport = "ssh"  # explicit — iterate writes this field
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport), \
         patch("satdeploy.iterate.resolve_provenance",
               return_value=("abcd", "local")):
        iterate.run_iterate(fake_config, fake_module, "controller")

    # Query the recorded row directly.
    import sqlite3
    conn = sqlite3.connect(fake_config.history_path)
    row = conn.execute(
        "SELECT transport, source, action, success, module FROM deployments "
        "WHERE app = ? ORDER BY id DESC LIMIT 1",
        ("controller",),
    ).fetchone()
    conn.close()
    assert row is not None, "iterate did not write a history row"
    transport, source, action, success, module = row
    assert transport == "ssh"
    assert source == "cli"
    assert action == "push"
    assert success == 1
    assert module == "som1"


# ---------------------------------------------------------------------------
# ABI gate
# ---------------------------------------------------------------------------

def test_run_iterate_raises_abi_error_when_lib_missing(
    fake_config, fake_module, mocked_transport, tmp_binary, tmp_path
):
    """The ABI gate must fire BEFORE we burn the transport connect+deploy
    budget. Assert neither was called on failure."""
    empty_sysroot = tmp_path / "empty-sysroot"
    (empty_sysroot / "usr" / "lib").mkdir(parents=True)  # exists, but no libs

    _READELF_OUT = (
        "Dynamic section:\n"
        " 0x0001 (NEEDED) Shared library: [libparam.so.3]\n"
    )
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport), \
         patch("subprocess.check_output", return_value=_READELF_OUT):
        with pytest.raises(errors.ABIError) as excinfo:
            iterate.run_iterate(
                fake_config, fake_module, "controller",
                sysroot=empty_sysroot,
            )

    assert excinfo.value.exit_code == errors.EABI
    assert "libparam.so.3" in excinfo.value.message
    mocked_transport.connect.assert_not_called()
    mocked_transport.deploy.assert_not_called()


def test_run_iterate_uses_satdeploy_sdk_env_var_as_sysroot(
    fake_config, fake_module, mocked_transport, sysroot_complete, monkeypatch
):
    """When --sysroot isn't passed, SATDEPLOY_SDK takes its place. Confirm
    the ABI check ran (readelf was invoked) against that path."""
    monkeypatch.setenv("SATDEPLOY_SDK", str(sysroot_complete))
    called_with = {}

    def _fake_check_output(cmd, *a, **kw):
        called_with["cmd"] = cmd
        return (
            "Dynamic section:\n"
            " 0x0001 (NEEDED) Shared library: [libparam.so.3]\n"
            " 0x0001 (NEEDED) Shared library: [libcsp.so]\n"
            " 0x0001 (NEEDED) Shared library: [libc.so.6]\n"
        )

    with patch("satdeploy.cli.get_transport", return_value=mocked_transport), \
         patch("subprocess.check_output", side_effect=_fake_check_output):
        iterate.run_iterate(fake_config, fake_module, "controller")

    assert called_with.get("cmd") is not None, "readelf was never invoked"
    assert "readelf" in called_with["cmd"][0]


def test_run_iterate_skips_abi_check_when_no_sysroot_anywhere(
    fake_config, fake_module, mocked_transport, monkeypatch
):
    """Without sysroot arg and without SATDEPLOY_SDK, iterate must not
    crash — it just skips the ABI check. The user sees a deploy with no
    upfront safety net; landmine is documented in errors.py caveats."""
    monkeypatch.delenv("SATDEPLOY_SDK", raising=False)
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport), \
         patch("satdeploy.iterate.resolve_provenance", return_value=("x", "local")), \
         patch("subprocess.check_output") as readelf:
        iterate.run_iterate(fake_config, fake_module, "controller")
    readelf.assert_not_called()


# ---------------------------------------------------------------------------
# Per-app flock — prevents concurrent iterate
# ---------------------------------------------------------------------------

def test_run_iterate_blocks_concurrent_invocation_for_same_app(
    fake_config, fake_module, mocked_transport, tmp_path
):
    """Second iterate for the same app while first still holds the lock
    must raise BusyError. We simulate the already-locked state by opening
    + locking the lock file ourselves before calling iterate."""
    import fcntl
    iterate.ITERATE_LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = iterate.ITERATE_LOCK_DIR / "iterate-controller.lock"
    lock_path.touch()
    holder = open(lock_path, "r+")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with patch("satdeploy.cli.get_transport", return_value=mocked_transport):
            with pytest.raises(errors.BusyError) as excinfo:
                iterate.run_iterate(fake_config, fake_module, "controller")
        assert excinfo.value.exit_code == errors.EBUSY
        mocked_transport.connect.assert_not_called()
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


def test_run_iterate_releases_lock_on_failure(
    fake_config, fake_module, mocked_transport, tmp_path
):
    """Landmine check: if iterate raises mid-run, the flock must release
    so a second attempt isn't falsely blocked. Without the try/finally in
    run_iterate, the failing test's flock would leak and the retry would
    see a spurious BusyError."""
    mocked_transport.deploy.side_effect = TransportError("boom: rsync error: error in socket IO (code 10)")
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport):
        with pytest.raises(errors.TransferError):
            iterate.run_iterate(fake_config, fake_module, "controller")

    # Second call should succeed (lock released). Reset mock side_effect.
    mocked_transport.deploy.side_effect = None
    mocked_transport.deploy.return_value = DeployResult(
        success=True, backup_path="/tmp/b.bak", file_hash="abcd",
    )
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport), \
         patch("satdeploy.iterate.resolve_provenance", return_value=("x", "local")):
        iterate.run_iterate(fake_config, fake_module, "controller")


# ---------------------------------------------------------------------------
# Transport failure → typed error routing
# ---------------------------------------------------------------------------

def test_transport_connect_failure_routes_through_errors_py(
    fake_config, fake_module, mocked_transport
):
    """A rsync-style connect failure must surface as TransferError,
    not generic TransportError. Closes the 'service won't restart'
    class of confusion (landmine #14)."""
    mocked_transport.connect.side_effect = TransportError(
        "rsync error: error in socket IO (code 10) at clientserver.c"
    )
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport):
        with pytest.raises(errors.TransferError) as excinfo:
            iterate.run_iterate(fake_config, fake_module, "controller")
    assert excinfo.value.exit_code == errors.ETRANSFER


def test_transport_deploy_result_success_false_still_raises(
    fake_config, fake_module, mocked_transport
):
    """Transport returns DeployResult(success=False) with error_message —
    iterate must route that through errors.py, not silently swallow."""
    mocked_transport.deploy.return_value = DeployResult(
        success=False,
        error_message="Failed to restart controller.service: Unit controller.service has failed",
    )
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport):
        with pytest.raises(errors.RestartError) as excinfo:
            iterate.run_iterate(fake_config, fake_module, "controller")
    assert excinfo.value.exit_code == errors.ERESTART


def test_unknown_transport_error_falls_back_to_unknown_error(
    fake_config, fake_module, mocked_transport
):
    mocked_transport.deploy.side_effect = TransportError("some weird unmatched error")
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport):
        with pytest.raises(errors.UnknownError) as excinfo:
            iterate.run_iterate(fake_config, fake_module, "controller")
    assert excinfo.value.exit_code == errors.EUNKNOWN


# ---------------------------------------------------------------------------
# --debug: gdbserver spawn + debuginfod start
# ---------------------------------------------------------------------------

def test_debug_path_starts_debuginfod_and_gdbserver(
    fake_config, fake_module, mocked_transport, tmp_path, monkeypatch
):
    """--debug success: both debuginfod.serve and transport._run_cmd get
    called; the returned result carries a debug URL."""
    monkeypatch.delenv("SATDEPLOY_SDK", raising=False)
    # Simulate SSH transport with _run_cmd; returns MainPID 12345 first,
    # then empty for the subsequent commands (pkill + nohup spawn).
    calls = []

    def _run_cmd(cmd):
        calls.append(cmd)
        if "systemctl show" in cmd:
            return "12345\n"
        return ""

    mocked_transport._run_cmd = _run_cmd

    with patch("satdeploy.cli.get_transport", return_value=mocked_transport), \
         patch("satdeploy.iterate.resolve_provenance", return_value=("x", "local")), \
         patch("satdeploy.iterate.debuginfod_module.serve", return_value=4242):
        result = iterate.run_iterate(
            fake_config, fake_module, "controller", debug=True,
        )

    assert result.debug_url is not None
    assert "http://" in result.debug_url
    # Confirm the gdbserver attach command was issued.
    assert any("gdbserver :9001" in c and "12345" in c for c in calls), calls


def test_debug_path_raises_debug_error_when_unit_not_running(
    fake_config, fake_module, mocked_transport, monkeypatch
):
    """gdbserver attach needs a live process. MainPID=0 means the service
    isn't running; iterate must surface this as EDEBUG, not attempt to
    attach to PID 0 (which would hang on target)."""
    monkeypatch.delenv("SATDEPLOY_SDK", raising=False)

    def _run_cmd(cmd):
        if "systemctl show" in cmd:
            return "0\n"
        return ""

    mocked_transport._run_cmd = _run_cmd

    with patch("satdeploy.cli.get_transport", return_value=mocked_transport), \
         patch("satdeploy.iterate.resolve_provenance", return_value=("x", "local")), \
         patch("satdeploy.iterate.debuginfod_module.serve", return_value=4242):
        with pytest.raises(errors.DebugError) as excinfo:
            iterate.run_iterate(
                fake_config, fake_module, "controller", debug=True,
            )
    assert "not running" in excinfo.value.message


def test_debug_path_raises_debug_error_when_transport_has_no_exec(
    fake_config, fake_module, mocked_transport, monkeypatch
):
    """CSP transport (or any transport without _run_cmd / execute) must
    raise a clear DebugError rather than crashing on AttributeError."""
    monkeypatch.delenv("SATDEPLOY_SDK", raising=False)
    # Strip the helper methods that _spawn_gdbserver_ssh probes for.
    if hasattr(mocked_transport, "_run_cmd"):
        delattr(mocked_transport, "_run_cmd")
    if hasattr(mocked_transport, "execute"):
        delattr(mocked_transport, "execute")
    mocked_transport.mock_add_spec(
        spec=["connect", "disconnect", "deploy", "rollback",
              "get_status", "list_backups", "get_logs"],
    )
    mocked_transport.deploy.return_value = DeployResult(
        success=True, backup_path="/tmp/b.bak", file_hash="abcd",
    )

    with patch("satdeploy.cli.get_transport", return_value=mocked_transport), \
         patch("satdeploy.iterate.resolve_provenance", return_value=("x", "local")), \
         patch("satdeploy.iterate.debuginfod_module.serve", return_value=4242):
        with pytest.raises(errors.DebugError) as excinfo:
            iterate.run_iterate(
                fake_config, fake_module, "controller", debug=True,
            )
    assert "SSH transport" in excinfo.value.fix_cmd


# ---------------------------------------------------------------------------
# Missing app / missing local file
# ---------------------------------------------------------------------------

def test_iterate_passes_dependency_ordered_services_to_transport(
    fake_config, fake_module, mocked_transport
):
    """The whole point of satdeploy over rsync-and-restart is that it
    stops services in dependents-first order and starts them in
    dependencies-first order, driven by config. Regression guard: if
    iterate ever stops passing `services=` into transport.deploy, the
    ordering contract silently reverts to "just the app's own service"
    and a deploy of `libparam` stops restarting the downstream services
    that depend on it.

    See design doc: stop order = dependents first, start order = deps first.
    """
    from satdeploy import cli as cli_module

    fake_services = [
        ("controller", "controller.service"),
        ("csp_server", "csp_server.service"),
    ]
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport), \
         patch("satdeploy.iterate.resolve_provenance", return_value=("x", "local")), \
         patch.object(cli_module, "get_services_to_manage", return_value=fake_services) as gsm:
        iterate.run_iterate(fake_config, fake_module, "controller")

    gsm.assert_called_once()
    # Confirm the same list reached transport.deploy without reordering or
    # dropping entries — the SSH transport is what actually uses this.
    call_kwargs = mocked_transport.deploy.call_args.kwargs
    assert call_kwargs.get("services") == fake_services


def test_missing_app_raises_unknown_error(
    fake_config, fake_module, mocked_transport
):
    """Regression: iterate previously referenced `config.path` which does
    not exist on the real Config class (it's `config_path`). With MagicMock
    not speccing the attribute the unit tests passed while the live CLI
    crashed with AttributeError. Spec-locked fixture + listing known apps
    in the message catches both problems."""
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport):
        with pytest.raises(errors.UnknownError) as excinfo:
            iterate.run_iterate(fake_config, fake_module, "nonexistent-app")
    assert "nonexistent-app" in excinfo.value.message
    # Error message must list known apps so the user isn't left guessing.
    assert "controller" in excinfo.value.message
    mocked_transport.connect.assert_not_called()


def test_missing_local_file_raises_unknown_error(
    fake_config, fake_module, mocked_transport, tmp_path
):
    with patch("satdeploy.cli.get_transport", return_value=mocked_transport):
        with pytest.raises(errors.UnknownError) as excinfo:
            iterate.run_iterate(
                fake_config, fake_module, "controller",
                local_override=str(tmp_path / "does-not-exist.elf"),
            )
    assert "not found" in excinfo.value.message.lower()
    mocked_transport.connect.assert_not_called()
