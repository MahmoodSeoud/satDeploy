"""Tests for `satdeploy validate` and the validations side-table.

Covers the spec from DX review 2026-04-23 Tier 1 decision #11:

  * `validate_command` round-trips through config (covered in test_config.py)
  * history.db `_migrate()` adds the `validations` table idempotently
  * `record_validation` / `has_pass_record` / `get_validation_history`
    behave as the gate expects, including (target, app, file_hash) keying
    so a PASS on som1 does NOT satisfy a gate query on som2 (R1 fleet
    contract — see memory `r1_fleet_preview_contract`).
  * `satdeploy validate <app>` records PASS on exit 0 and FAIL on nonzero
  * The CLI exits nonzero on FAIL so `&&` chains break.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.history import (
    History,
    ValidationRecord,
    VALIDATION_FAIL,
    VALIDATION_PASS,
)
from satdeploy.transport.base import TransportError
from satdeploy.transport.local import LocalTransport
from satdeploy.transport.ssh import SSHTransport


# ---------------------------------------------------------------------------
# History migration + persistence
# ---------------------------------------------------------------------------

class TestHistoryValidationsTable:
    def test_init_creates_validations_table(self, tmp_path):
        db_path = tmp_path / "history.db"
        h = History(db_path)
        h.init_db()

        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='validations'"
        )
        assert cur.fetchone() is not None
        conn.close()

    def test_validations_table_has_expected_columns(self, tmp_path):
        db_path = tmp_path / "history.db"
        h = History(db_path)
        h.init_db()

        conn = sqlite3.connect(db_path)
        cur = conn.execute("PRAGMA table_info(validations)")
        cols = {row[1] for row in cur.fetchall()}
        conn.close()
        assert {
            "id", "target", "app", "file_hash", "status", "exit_code",
            "duration_ms", "command", "stdout", "stderr", "timestamp",
        } == cols

    def test_init_db_is_idempotent(self, tmp_path):
        """Running init_db twice must not raise — _migrate path."""
        db_path = tmp_path / "history.db"
        h = History(db_path)
        h.init_db()
        h.init_db()  # would raise if CREATE TABLE were not IF NOT EXISTS

        # And after the second init, we can still write/read.
        h.record_validation(ValidationRecord(
            target="som1", app="controller", file_hash="abc12345",
            status=VALIDATION_PASS, exit_code=0, duration_ms=10, command="true",
        ))
        records = h.get_validation_history(app="controller")
        assert len(records) == 1

    def test_migrate_adds_validations_table_to_old_db(self, tmp_path):
        """An existing deployments-only DB must gain `validations` on init."""
        db_path = tmp_path / "history.db"
        # Create a deployments table without the validations side table.
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE deployments (
                id INTEGER PRIMARY KEY,
                module TEXT NOT NULL DEFAULT 'default',
                app TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                git_hash TEXT,
                file_hash TEXT NOT NULL,
                remote_path TEXT NOT NULL,
                backup_path TEXT,
                action TEXT NOT NULL,
                success INTEGER NOT NULL,
                error_message TEXT,
                service_hash TEXT,
                vmem_cleared INTEGER NOT NULL DEFAULT 0,
                provenance_source TEXT,
                transport TEXT,
                source TEXT NOT NULL DEFAULT 'cli'
            )
        """)
        conn.commit()
        conn.close()

        # init_db should add `validations` without touching `deployments`.
        h = History(db_path)
        h.init_db()

        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='validations'"
        )
        assert cur.fetchone() is not None
        conn.close()


class TestRecordValidation:
    @pytest.fixture
    def history(self, tmp_path):
        h = History(tmp_path / "history.db")
        h.init_db()
        return h

    def test_record_round_trips(self, history):
        history.record_validation(ValidationRecord(
            target="som1", app="controller", file_hash="deadbeef",
            status=VALIDATION_PASS, exit_code=0, duration_ms=120,
            command="./tests/run.sh", stdout="ok\n", stderr="",
        ))
        records = history.get_validation_history(app="controller")
        assert len(records) == 1
        r = records[0]
        assert r.target == "som1"
        assert r.app == "controller"
        assert r.file_hash == "deadbeef"
        assert r.status == VALIDATION_PASS
        assert r.exit_code == 0
        assert r.duration_ms == 120
        assert r.command == "./tests/run.sh"
        assert r.stdout == "ok\n"

    def test_has_pass_record_returns_true_after_pass(self, history):
        history.record_validation(ValidationRecord(
            target="som1", app="controller", file_hash="deadbeef",
            status=VALIDATION_PASS, exit_code=0, duration_ms=10,
            command="true",
        ))
        assert history.has_pass_record(
            target="som1", app="controller", file_hash="deadbeef"
        )

    def test_has_pass_record_false_when_only_fail(self, history):
        history.record_validation(ValidationRecord(
            target="som1", app="controller", file_hash="deadbeef",
            status=VALIDATION_FAIL, exit_code=1, duration_ms=10,
            command="false",
        ))
        assert not history.has_pass_record(
            target="som1", app="controller", file_hash="deadbeef"
        )

    def test_pass_on_one_target_does_not_satisfy_another(self, history):
        """R1 fleet contract — the gate must key by target_name too."""
        history.record_validation(ValidationRecord(
            target="som1", app="controller", file_hash="deadbeef",
            status=VALIDATION_PASS, exit_code=0, duration_ms=10,
            command="true",
        ))
        assert history.has_pass_record(
            target="som1", app="controller", file_hash="deadbeef"
        )
        assert not history.has_pass_record(
            target="flight", app="controller", file_hash="deadbeef"
        )

    def test_pass_for_one_hash_does_not_satisfy_another_hash(self, history):
        history.record_validation(ValidationRecord(
            target="som1", app="controller", file_hash="aaaa1111",
            status=VALIDATION_PASS, exit_code=0, duration_ms=10,
            command="true",
        ))
        assert history.has_pass_record(
            target="som1", app="controller", file_hash="aaaa1111"
        )
        assert not history.has_pass_record(
            target="som1", app="controller", file_hash="bbbb2222"
        )

    def test_get_validation_history_filters_by_target(self, history):
        for target in ("som1", "som2", "som1"):
            history.record_validation(ValidationRecord(
                target=target, app="controller", file_hash="deadbeef",
                status=VALIDATION_PASS, exit_code=0, duration_ms=10,
                command="true",
            ))
        som1_only = history.get_validation_history(app="controller", target="som1")
        assert len(som1_only) == 2
        all_records = history.get_validation_history(app="controller")
        assert len(all_records) == 3


# ---------------------------------------------------------------------------
# Transport.exec_command
# ---------------------------------------------------------------------------

class TestLocalExecCommand:
    """Local transport runs commands locally for the demo / chroot path."""

    def test_exec_returns_exit_zero_on_true(self, tmp_path):
        transport = LocalTransport(
            target_dir=str(tmp_path / "t"),
            backup_dir=str(tmp_path / "b"),
        )
        transport.connect()
        try:
            code, stdout, stderr = transport.exec_command("true")
        finally:
            transport.disconnect()
        assert code == 0

    def test_exec_returns_nonzero_on_false(self, tmp_path):
        transport = LocalTransport(
            target_dir=str(tmp_path / "t"),
            backup_dir=str(tmp_path / "b"),
        )
        transport.connect()
        try:
            code, _, _ = transport.exec_command("false")
        finally:
            transport.disconnect()
        assert code != 0

    def test_exec_captures_stdout_and_stderr(self, tmp_path):
        transport = LocalTransport(
            target_dir=str(tmp_path / "t"),
            backup_dir=str(tmp_path / "b"),
        )
        transport.connect()
        try:
            code, stdout, stderr = transport.exec_command(
                "echo out; echo err 1>&2"
            )
        finally:
            transport.disconnect()
        assert code == 0
        assert "out" in stdout
        assert "err" in stderr

    def test_exec_raises_transport_error_on_timeout(self, tmp_path):
        transport = LocalTransport(
            target_dir=str(tmp_path / "t"),
            backup_dir=str(tmp_path / "b"),
        )
        transport.connect()
        try:
            with pytest.raises(TransportError):
                transport.exec_command("sleep 5", timeout=0.1)
        finally:
            transport.disconnect()


class TestSSHTransportExecCommandUnsupported:
    """SSH transport requires connection — assert API surface, not behavior."""

    def test_exec_raises_when_not_connected(self):
        t = SSHTransport(host="h", user="u", backup_dir="/tmp/b")
        with pytest.raises(TransportError):
            t.exec_command("true")


# ---------------------------------------------------------------------------
# `satdeploy validate` CLI integration (local transport)
# ---------------------------------------------------------------------------

def _validate_config(
    tmp_path: Path,
    *,
    validate_command: str,
    binary_contents: bytes = b"\x7fELF stub",
) -> dict:
    target_dir = tmp_path / "som1"
    target_dir.mkdir()
    bin_dir = tmp_path / "src"
    bin_dir.mkdir()
    binary = bin_dir / "controller"
    binary.write_bytes(binary_contents)
    binary.chmod(0o755)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump({
        "default_target": "som1",
        "targets": {
            "som1": {
                "transport": "local",
                "target_dir": str(target_dir),
            },
        },
        "apps": {
            "controller": {
                "local": str(binary),
                "remote": "/bin/controller",
                "validate_command": validate_command,
            },
        },
    }))
    return {
        "config_path": config_path,
        "target_dir": target_dir,
        "binary": binary,
    }


def _invoke(args):
    return CliRunner().invoke(main, args, catch_exceptions=False)


class TestValidateCli:
    def test_validate_passes_records_pass_and_exits_zero(self, tmp_path):
        env = _validate_config(tmp_path, validate_command="true")
        result = _invoke([
            "validate", "controller",
            "--config", str(env["config_path"]),
        ])
        assert result.exit_code == 0, result.output
        assert "PASS" in result.output

        history = History(env["config_path"].parent / "history.db")
        history.init_db()
        records = history.get_validation_history(app="controller")
        assert len(records) == 1
        assert records[0].status == VALIDATION_PASS
        assert records[0].target == "som1"

    def test_validate_fails_records_fail_and_exits_nonzero(self, tmp_path):
        env = _validate_config(tmp_path, validate_command="false")
        result = _invoke([
            "validate", "controller",
            "--config", str(env["config_path"]),
        ])
        assert result.exit_code != 0
        assert "FAIL" in result.output

        history = History(env["config_path"].parent / "history.db")
        history.init_db()
        records = history.get_validation_history(app="controller")
        assert len(records) == 1
        assert records[0].status == VALIDATION_FAIL
        assert records[0].exit_code != 0

    def test_validate_refuses_when_no_validate_command_configured(self, tmp_path):
        target_dir = tmp_path / "som1"
        target_dir.mkdir()
        bin_dir = tmp_path / "src"
        bin_dir.mkdir()
        binary = bin_dir / "lib.so"
        binary.write_bytes(b"\x7fELF")

        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({
            "default_target": "som1",
            "targets": {
                "som1": {"transport": "local", "target_dir": str(target_dir)},
            },
            "apps": {
                "lib": {"local": str(binary), "remote": "/lib/lib.so"},
            },
        }))

        result = _invoke([
            "validate", "lib",
            "--config", str(config_path),
        ])
        assert result.exit_code != 0
        assert "validate_command" in result.output

    def test_validate_records_target_name_for_fleet(self, tmp_path):
        """Two targets — validate against som2 records target='som2'."""
        som1 = tmp_path / "som1"
        som2 = tmp_path / "som2"
        som1.mkdir()
        som2.mkdir()
        bin_dir = tmp_path / "src"
        bin_dir.mkdir()
        binary = bin_dir / "controller"
        binary.write_bytes(b"\x7fELF stub")
        binary.chmod(0o755)

        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump({
            "default_target": "som1",
            "targets": {
                "som1": {"transport": "local", "target_dir": str(som1)},
                "som2": {"transport": "local", "target_dir": str(som2)},
            },
            "apps": {
                "controller": {
                    "local": str(binary),
                    "remote": "/bin/controller",
                    "validate_command": "true",
                },
            },
        }))

        result = _invoke([
            "validate", "controller",
            "--target", "som2",
            "--config", str(config_path),
        ])
        assert result.exit_code == 0, result.output

        history = History(config_path.parent / "history.db")
        history.init_db()

        # PASS rows exist for som2 only — som1 was never validated.
        from satdeploy.hash import compute_file_hash
        h = compute_file_hash(str(binary))
        assert history.has_pass_record(target="som2", app="controller", file_hash=h)
        assert not history.has_pass_record(target="som1", app="controller", file_hash=h)
