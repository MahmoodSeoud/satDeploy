"""Tests for the history database module."""

import sqlite3
from datetime import datetime

import pytest

from satdeploy.history import History, DeploymentRecord


class TestHistoryInit:
    """Tests for History initialization."""

    def test_history_creates_db_file(self, tmp_path):
        """History creates the database file."""
        db_path = tmp_path / ".satdeploy" / "history.db"
        history = History(db_path)
        history.init_db()
        assert db_path.exists()

    def test_history_creates_parent_directory(self, tmp_path):
        """History creates parent directory if it doesn't exist."""
        db_path = tmp_path / ".satdeploy" / "history.db"
        history = History(db_path)
        history.init_db()
        assert db_path.parent.exists()

    def test_history_creates_deployments_table(self, tmp_path):
        """History creates the deployments table."""
        db_path = tmp_path / ".satdeploy" / "history.db"
        history = History(db_path)
        history.init_db()

        conn = sqlite3.connect(db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='deployments'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_history_table_has_correct_columns(self, tmp_path):
        """Deployments table has all required columns."""
        db_path = tmp_path / ".satdeploy" / "history.db"
        history = History(db_path)
        history.init_db()

        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(deployments)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        expected = {
            "id", "app", "timestamp", "git_hash", "binary_hash",
            "remote_path", "backup_path", "action", "success", "error_message",
            "module", "service_hash", "vmem_cleared"
        }
        assert expected == columns


class TestDeploymentRecordModule:
    """Tests for module field in DeploymentRecord."""

    def test_deployment_record_has_module_field(self):
        """DeploymentRecord has a module field."""
        record = DeploymentRecord(
            module="som1",
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
        )
        assert record.module == "som1"

    def test_module_defaults_to_default(self):
        """Module field defaults to 'default' for backward compatibility."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
        )
        assert record.module == "default"

    def test_deployment_record_has_service_hash_field(self):
        """DeploymentRecord has service_hash field for tracking service file version."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
            service_hash="abcd1234",
        )
        assert record.service_hash == "abcd1234"

    def test_service_hash_defaults_to_none(self):
        """Service hash defaults to None for apps without service templates."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
        )
        assert record.service_hash is None

    def test_deployment_record_has_vmem_cleared_field(self):
        """DeploymentRecord has vmem_cleared field."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
            vmem_cleared=True,
        )
        assert record.vmem_cleared is True

    def test_vmem_cleared_defaults_to_false(self):
        """vmem_cleared defaults to False."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
        )
        assert record.vmem_cleared is False


class TestHistoryRecording:
    """Tests for recording deployments."""

    @pytest.fixture
    def history(self, tmp_path):
        db_path = tmp_path / ".satdeploy" / "history.db"
        h = History(db_path)
        h.init_db()
        return h

    def test_record_push_success(self, history):
        """Record a successful push deployment."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
            backup_path="/opt/satdeploy/backups/controller/20240115-143022.bak",
        )
        history.record(record)

        records = history.get_history("controller")
        assert len(records) == 1
        assert records[0].app == "controller"
        assert records[0].binary_hash == "a1b2c3d4"
        assert records[0].action == "push"
        assert records[0].success is True

    def test_record_push_failure(self, history):
        """Record a failed push deployment."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=False,
            error_message="Connection refused",
        )
        history.record(record)

        records = history.get_history("controller")
        assert len(records) == 1
        assert records[0].success is False
        assert records[0].error_message == "Connection refused"

    def test_record_rollback(self, history):
        """Record a rollback operation."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="rollback",
            success=True,
            backup_path="/opt/satdeploy/backups/controller/20240115-143022.bak",
        )
        history.record(record)

        records = history.get_history("controller")
        assert len(records) == 1
        assert records[0].action == "rollback"

    def test_record_with_git_hash(self, history):
        """Record includes git hash when available."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
            git_hash="abc123def456",
        )
        history.record(record)

        records = history.get_history("controller")
        assert records[0].git_hash == "abc123def456"

    def test_record_adds_timestamp(self, history):
        """Records get a timestamp automatically."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
        )
        history.record(record)

        records = history.get_history("controller")
        assert records[0].timestamp is not None

    def test_record_stores_module(self, history):
        """Record stores and retrieves module field."""
        record = DeploymentRecord(
            module="som1",
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
        )
        history.record(record)

        records = history.get_history("controller")
        assert records[0].module == "som1"

    def test_record_stores_service_hash(self, history):
        """Record stores and retrieves service_hash field."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
            service_hash="servicehash123",
        )
        history.record(record)

        records = history.get_history("controller")
        assert records[0].service_hash == "servicehash123"

    def test_record_stores_vmem_cleared(self, history):
        """Record stores and retrieves vmem_cleared field."""
        record = DeploymentRecord(
            app="controller",
            binary_hash="a1b2c3d4",
            remote_path="/opt/disco/bin/controller",
            action="push",
            success=True,
            vmem_cleared=True,
        )
        history.record(record)

        records = history.get_history("controller")
        assert records[0].vmem_cleared is True


class TestHistoryQuery:
    """Tests for querying deployment history."""

    @pytest.fixture
    def history_with_records(self, tmp_path):
        db_path = tmp_path / ".satdeploy" / "history.db"
        h = History(db_path)
        h.init_db()

        # Add some records
        for i, app in enumerate(["controller", "controller", "csp_server"]):
            record = DeploymentRecord(
                app=app,
                binary_hash=f"hash{i}",
                remote_path=f"/path/{app}",
                action="push",
                success=True,
            )
            h.record(record)

        return h

    def test_get_history_filters_by_app(self, history_with_records):
        """Get history for specific app."""
        records = history_with_records.get_history("controller")
        assert len(records) == 2
        assert all(r.app == "controller" for r in records)

    def test_get_history_returns_newest_first(self, history_with_records):
        """History is returned newest first."""
        records = history_with_records.get_history("controller")
        assert records[0].binary_hash == "hash1"  # Second record added
        assert records[1].binary_hash == "hash0"  # First record added

    def test_get_history_with_limit(self, history_with_records):
        """Get history with limit."""
        records = history_with_records.get_history("controller", limit=1)
        assert len(records) == 1

    def test_get_all_history(self, history_with_records):
        """Get history for all apps."""
        records = history_with_records.get_all_history()
        assert len(records) == 3

    def test_get_last_deployment(self, history_with_records):
        """Get the most recent deployment for an app."""
        record = history_with_records.get_last_deployment("controller")
        assert record is not None
        assert record.binary_hash == "hash1"

    def test_get_last_deployment_returns_none_for_unknown_app(self, history_with_records):
        """Get last deployment returns None for unknown app."""
        record = history_with_records.get_last_deployment("unknown")
        assert record is None


class TestModuleState:
    """Tests for module state queries."""

    @pytest.fixture
    def history_with_modules(self, tmp_path):
        """History with records for multiple modules."""
        db_path = tmp_path / ".satdeploy" / "history.db"
        h = History(db_path)
        h.init_db()

        # Deploy controller to som1
        h.record(DeploymentRecord(
            module="som1",
            app="controller",
            binary_hash="hash_ctrl_som1_v1",
            remote_path="/path/controller",
            action="push",
            success=True,
        ))
        # Update controller on som1
        h.record(DeploymentRecord(
            module="som1",
            app="controller",
            binary_hash="hash_ctrl_som1_v2",
            remote_path="/path/controller",
            action="push",
            success=True,
        ))
        # Deploy csp_server to som1
        h.record(DeploymentRecord(
            module="som1",
            app="csp_server",
            binary_hash="hash_csp_som1",
            remote_path="/path/csp_server",
            action="push",
            success=True,
        ))
        # Deploy controller to som2
        h.record(DeploymentRecord(
            module="som2",
            app="controller",
            binary_hash="hash_ctrl_som2",
            remote_path="/path/controller",
            action="push",
            success=True,
        ))

        return h

    def test_get_module_state_returns_last_state_per_app(self, history_with_modules):
        """get_module_state returns the last deployment for each app on the module."""
        state = history_with_modules.get_module_state("som1")

        # Should have 2 apps
        assert len(state) == 2
        assert "controller" in state
        assert "csp_server" in state

        # Controller should be v2 (most recent)
        assert state["controller"].binary_hash == "hash_ctrl_som1_v2"
        assert state["csp_server"].binary_hash == "hash_csp_som1"

    def test_get_module_state_empty_for_unknown_module(self, history_with_modules):
        """get_module_state returns empty dict for unknown module."""
        state = history_with_modules.get_module_state("unknown")
        assert state == {}

    def test_get_module_state_separate_modules(self, history_with_modules):
        """get_module_state returns separate state per module."""
        som1_state = history_with_modules.get_module_state("som1")
        som2_state = history_with_modules.get_module_state("som2")

        # som1 has 2 apps
        assert len(som1_state) == 2

        # som2 has only 1 app
        assert len(som2_state) == 1
        assert "controller" in som2_state
        assert som2_state["controller"].binary_hash == "hash_ctrl_som2"
