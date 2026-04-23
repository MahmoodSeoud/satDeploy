"""Tests for satdeploy.doctor — pre-flight check command.

Born from the 2026-04-23 DX review. Tests cover each check function in
isolation (via subprocess mocking where needed) plus the orchestrator and
the CLI exit code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from satdeploy import doctor
from satdeploy.cli import main
from satdeploy.config import AppConfig, Config, ModuleConfig
from satdeploy.doctor import CheckResult, CheckStatus, DoctorContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_binary(tmp_path):
    p = tmp_path / "controller"
    p.write_bytes(b"\x7fELF" + b"\x00" * 256)
    return p


@pytest.fixture
def fake_config(tmp_path, tmp_binary):
    cfg = MagicMock(spec=Config)
    cfg.config_path = tmp_path / "config.yaml"
    cfg.history_path = tmp_path / "history.db"
    cfg.get_backup_dir = lambda name: "/opt/satdeploy/backups"
    cfg.get_all_app_names = lambda: ["controller", "libparam"]
    cfg.get_app = lambda name: {
        "controller": AppConfig(
            name="controller",
            local=str(tmp_binary),
            remote="/opt/app/bin/controller",
            service="controller.service",
        ),
        "libparam": AppConfig(
            name="libparam",
            local=str(tmp_binary.parent / "libparam.so"),
            remote="/usr/lib/libparam.so",
            service=None,
        ),
    }.get(name)
    cfg.apps = {"controller": None, "libparam": None}  # iter list only
    return cfg


@pytest.fixture
def ssh_module():
    return ModuleConfig(name="pi", transport="ssh", host="mseo-pi", user="mseo")


@pytest.fixture
def root_module():
    return ModuleConfig(name="root", transport="ssh", host="mseo-pi", user="root")


@pytest.fixture
def local_module():
    return ModuleConfig(name="demo", transport="local", target_dir="/tmp/demo")


# ---------------------------------------------------------------------------
# Individual check: SSH
# ---------------------------------------------------------------------------

def test_check_ssh_auto_pass_for_non_ssh_transport(fake_config, local_module):
    ctx = DoctorContext(fake_config, local_module, ["controller"], "all")
    results = doctor.check_ssh(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.PASS
    assert "not applicable" in results[0].message


def test_check_ssh_reports_success_with_elapsed(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, [], "all")
    proc = MagicMock()
    proc.returncode = 0
    proc.stderr = b""
    with patch("satdeploy.doctor.subprocess.run", return_value=proc):
        results = doctor.check_ssh(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.PASS
    assert "mseo@mseo-pi" in results[0].message
    assert "connected" in results[0].message


def test_check_ssh_failure_includes_ssh_copy_id_fix(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, [], "all")
    proc = MagicMock()
    proc.returncode = 255
    proc.stderr = b"Permission denied (publickey)"
    with patch("satdeploy.doctor.subprocess.run", return_value=proc):
        results = doctor.check_ssh(ctx)
    assert results[0].status == CheckStatus.FAIL
    assert "Permission denied" in results[0].message
    assert "ssh-copy-id" in (results[0].fix_cmd or "")


def test_check_ssh_timeout(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, [], "all")
    with patch("satdeploy.doctor.subprocess.run",
               side_effect=subprocess.TimeoutExpired("ssh", 10)):
        results = doctor.check_ssh(ctx)
    assert results[0].status == CheckStatus.FAIL
    assert "timeout" in results[0].message.lower()
    assert "reachable" in (results[0].fix_cmd or "").lower()


def test_check_ssh_unset_host_fails_before_connecting(fake_config):
    module = ModuleConfig(name="bad", transport="ssh", host=None, user="mseo")
    ctx = DoctorContext(fake_config, module, [], "all")
    with patch("satdeploy.doctor.subprocess.run") as run:
        results = doctor.check_ssh(ctx)
        # Don't even invoke ssh — bail before the subprocess call.
        assert run.call_count == 0
    assert results[0].status == CheckStatus.FAIL
    assert "host is unset" in results[0].message.lower()


# ---------------------------------------------------------------------------
# Individual check: sudo
# ---------------------------------------------------------------------------

def test_check_sudo_skipped_when_no_service_configured(tmp_path, ssh_module):
    cfg = MagicMock(spec=Config)
    cfg.config_path = tmp_path / "c.yaml"
    cfg.get_app = lambda name: AppConfig(
        name=name, local="/x", remote="/y", service=None
    )
    cfg.get_all_app_names = lambda: ["libparam"]
    ctx = DoctorContext(cfg, ssh_module, ["libparam"], "iterate")
    results = doctor.check_sudo(ctx)
    assert results == []  # no service = no sudo needed


def test_check_sudo_auto_passes_for_root_user(fake_config, root_module):
    ctx = DoctorContext(fake_config, root_module, ["controller"], "iterate")
    results = doctor.check_sudo(ctx)
    assert results[0].status == CheckStatus.PASS
    assert "root" in results[0].message.lower()


def test_check_sudo_passwordless_pass(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, ["controller"], "iterate")
    proc = MagicMock()
    proc.returncode = 0
    with patch("satdeploy.doctor.subprocess.run", return_value=proc):
        results = doctor.check_sudo(ctx)
    assert results[0].status == CheckStatus.PASS


def test_check_sudo_fail_emits_sudoers_fix(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, ["controller"], "iterate")
    proc = MagicMock()
    proc.returncode = 1
    with patch("satdeploy.doctor.subprocess.run", return_value=proc):
        results = doctor.check_sudo(ctx)
    assert results[0].status == CheckStatus.FAIL
    assert "/etc/sudoers.d/mseo" in (results[0].fix_cmd or "")


# ---------------------------------------------------------------------------
# Individual check: remote backup dir
# ---------------------------------------------------------------------------

def test_check_remote_backup_dir_pass(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, ["controller"], "all")
    proc = MagicMock()
    proc.returncode = 0
    with patch("satdeploy.doctor.subprocess.run", return_value=proc):
        results = doctor.check_remote_backup_dir(ctx)
    assert results[0].status == CheckStatus.PASS


def test_check_remote_backup_dir_missing_suggests_mkdir(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, ["controller"], "all")
    # First call (test -d && test -w) fails, second (test -d alone) also fails → missing
    proc_fail = MagicMock(returncode=1)
    with patch("satdeploy.doctor.subprocess.run",
               side_effect=[proc_fail, proc_fail]):
        results = doctor.check_remote_backup_dir(ctx)
    assert results[0].status == CheckStatus.FAIL
    assert "does not exist" in results[0].message
    assert "mkdir -p" in (results[0].fix_cmd or "")


def test_check_remote_backup_dir_unwritable_suggests_chown(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, ["controller"], "all")
    # First call (combined) fails, second (exists?) succeeds → exists but unwritable
    proc_fail = MagicMock(returncode=1)
    proc_exists = MagicMock(returncode=0)
    with patch("satdeploy.doctor.subprocess.run",
               side_effect=[proc_fail, proc_exists]):
        results = doctor.check_remote_backup_dir(ctx)
    assert results[0].status == CheckStatus.FAIL
    assert "can't write" in results[0].message.lower()
    assert "chown" in (results[0].fix_cmd or "")


# ---------------------------------------------------------------------------
# Individual check: local files
# ---------------------------------------------------------------------------

def test_check_local_files_pass(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, ["controller"], "iterate")
    results = doctor.check_local_files(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.PASS
    assert "controller" in results[0].message


def test_check_local_files_missing(fake_config, ssh_module):
    # libparam's local path doesn't exist in the fixture
    ctx = DoctorContext(fake_config, ssh_module, ["libparam"], "iterate")
    results = doctor.check_local_files(ctx)
    assert results[0].status == CheckStatus.FAIL
    assert "does not exist" in results[0].message


def test_check_local_files_unknown_app_lists_known(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, ["ghost"], "iterate")
    results = doctor.check_local_files(ctx)
    assert results[0].status == CheckStatus.FAIL
    assert "ghost" in results[0].message
    assert "controller" in results[0].message  # lists known apps


# ---------------------------------------------------------------------------
# Individual check: systemd units
# ---------------------------------------------------------------------------

def test_check_systemd_units_pass(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, ["controller"], "iterate")
    proc = MagicMock(returncode=0)
    with patch("satdeploy.doctor.subprocess.run", return_value=proc):
        results = doctor.check_systemd_units(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.PASS


def test_check_systemd_units_skips_apps_without_service(fake_config, ssh_module):
    # libparam has service=None
    ctx = DoctorContext(fake_config, ssh_module, ["libparam"], "iterate")
    results = doctor.check_systemd_units(ctx)
    assert results == []


def test_check_systemd_units_missing_emits_daemon_reload(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, ["controller"], "iterate")
    proc = MagicMock(returncode=1)
    with patch("satdeploy.doctor.subprocess.run", return_value=proc):
        results = doctor.check_systemd_units(ctx)
    assert results[0].status == CheckStatus.FAIL
    assert "daemon-reload" in (results[0].fix_cmd or "")


# ---------------------------------------------------------------------------
# Individual check: watchdog
# ---------------------------------------------------------------------------

def test_check_watchdog_pass(fake_config, ssh_module):
    ctx = DoctorContext(fake_config, ssh_module, [], "watch")
    results = doctor.check_watchdog(ctx)
    assert results[0].status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# Individual check: sysroot
# ---------------------------------------------------------------------------

def test_check_sysroot_warns_when_unset(fake_config, ssh_module, monkeypatch):
    monkeypatch.delenv("SATDEPLOY_SDK", raising=False)
    ctx = DoctorContext(fake_config, ssh_module, [], "debug")
    results = doctor.check_sysroot(ctx)
    assert results[0].status == CheckStatus.WARN
    assert "SATDEPLOY_SDK" in (results[0].fix_cmd or "")


def test_check_sysroot_fails_when_not_a_directory(fake_config, ssh_module, monkeypatch):
    monkeypatch.setenv("SATDEPLOY_SDK", "/does/not/exist")
    ctx = DoctorContext(fake_config, ssh_module, [], "debug")
    results = doctor.check_sysroot(ctx)
    assert results[0].status == CheckStatus.FAIL
    assert "not a directory" in results[0].message


def test_check_sysroot_passes_with_valid_dir(tmp_path, fake_config, ssh_module, monkeypatch):
    monkeypatch.setenv("SATDEPLOY_SDK", str(tmp_path))
    ctx = DoctorContext(fake_config, ssh_module, [], "debug")
    results = doctor.check_sysroot(ctx)
    assert results[0].status == CheckStatus.PASS


# ---------------------------------------------------------------------------
# MODE_CHECKS dispatch
# ---------------------------------------------------------------------------

def test_debug_mode_includes_gdbserver_and_sysroot():
    checks = doctor.MODE_CHECKS["debug"]
    names = {fn.__name__ for fn in checks}
    assert "check_gdbserver_on_target" in names
    assert "check_sysroot" in names
    assert "check_debuginfod_local" in names


def test_iterate_mode_does_not_include_debug_checks():
    checks = doctor.MODE_CHECKS["iterate"]
    names = {fn.__name__ for fn in checks}
    assert "check_gdbserver_on_target" not in names
    assert "check_sysroot" not in names


def test_watch_mode_adds_watchdog_check():
    checks = doctor.MODE_CHECKS["watch"]
    assert doctor.check_watchdog in checks


# ---------------------------------------------------------------------------
# run_doctor orchestrator
# ---------------------------------------------------------------------------

def test_run_doctor_aggregates_and_emits(fake_config, ssh_module):
    # Bypass SSH-dependent checks by using local transport.
    local_module = ModuleConfig(name="demo", transport="local", target_dir="/tmp")
    emitted: list[CheckResult] = []
    summary = doctor.run_doctor(
        fake_config, local_module, ["controller"], mode="iterate",
        on_result=emitted.append,
    )
    # Every emitted result contributes to exactly one counter.
    total = summary.passed + summary.warned + summary.failed
    assert total == len(emitted)
    assert all(isinstance(r, CheckResult) for r in emitted)


def test_run_doctor_summary_ok_when_no_fails(fake_config):
    local_module = ModuleConfig(name="demo", transport="local", target_dir="/tmp")
    summary = doctor.run_doctor(fake_config, local_module, ["controller"], mode="iterate")
    # Local transport + existing tmp binary + no service? wait controller has a service.
    # Let's verify we don't get a confusing fail — doctor should skip non-ssh systemd checks.
    # The fail case: local file not found for libparam. But we only pass controller.
    assert summary.failed == 0


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

def test_cli_doctor_exit_zero_on_pass(tmp_path, tmp_binary):
    """End-to-end CliRunner check: all-pass exits 0."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"""
name: demo
transport: local
target_dir: {tmp_path}/target
backup_dir: {tmp_path}/backups
apps:
  controller:
    local: {tmp_binary}
    remote: /app/controller
    service: null
""")
    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--config", str(config_file), "--for", "iterate"])
    assert result.exit_code == 0, f"Output: {result.output}"
    assert "passed" in result.output


def test_cli_doctor_exit_one_on_fail(tmp_path):
    """Missing local file should produce exit 1 and show a fix command."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(f"""
name: demo
transport: local
target_dir: {tmp_path}/target
backup_dir: {tmp_path}/backups
apps:
  controller:
    local: /does/not/exist/controller
    remote: /app/controller
    service: null
""")
    runner = CliRunner()
    result = runner.invoke(main, ["doctor", "--config", str(config_file), "--for", "iterate"])
    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert "failed" in result.output
