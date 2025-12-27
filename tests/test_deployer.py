"""Tests for deployer module."""

import hashlib
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch, call

import pytest

from satdeploy.deployer import Deployer, DeployResult


class TestBinaryHash:
    """Test binary hash computation."""

    def test_compute_hash_returns_8_chars(self, tmp_path):
        """Should return first 8 chars of sha256 hash."""
        binary = tmp_path / "test_binary"
        binary.write_bytes(b"test content")

        deployer = Deployer(
            ssh=Mock(),
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        hash_val = deployer.compute_hash(str(binary))

        assert len(hash_val) == 8
        assert hash_val.isalnum()

    def test_compute_hash_is_deterministic(self, tmp_path):
        """Same file should produce same hash."""
        binary = tmp_path / "test_binary"
        binary.write_bytes(b"test content")

        deployer = Deployer(
            ssh=Mock(),
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        hash1 = deployer.compute_hash(str(binary))
        hash2 = deployer.compute_hash(str(binary))

        assert hash1 == hash2

    def test_compute_hash_different_for_different_files(self, tmp_path):
        """Different files should produce different hashes."""
        binary1 = tmp_path / "binary1"
        binary1.write_bytes(b"content 1")
        binary2 = tmp_path / "binary2"
        binary2.write_bytes(b"content 2")

        deployer = Deployer(
            ssh=Mock(),
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        hash1 = deployer.compute_hash(str(binary1))
        hash2 = deployer.compute_hash(str(binary2))

        assert hash1 != hash2


class TestBackup:
    """Test backup creation."""

    def test_backup_creates_backup_directory(self):
        """Should create backup directory if it doesn't exist."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="abc12345def67890  /opt/disco/bin/controller\n")
        mock_ssh.file_exists.return_value = True

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        deployer.backup("controller", "/opt/disco/bin/controller")

        # Should have called mkdir -p
        mkdir_calls = [c for c in mock_ssh.run.call_args_list if "mkdir" in str(c)]
        assert len(mkdir_calls) > 0

    def test_backup_copies_current_binary(self):
        """Should copy current binary to backup location."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="abc12345def67890  /opt/disco/bin/controller\n")
        mock_ssh.file_exists.return_value = True

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        backup_path = deployer.backup("controller", "/opt/disco/bin/controller")

        # Should have called cp
        copy_calls = [c for c in mock_ssh.run.call_args_list if "cp" in str(c)]
        assert len(copy_calls) > 0
        assert "/opt/disco/bin/controller" in str(copy_calls[-1])

    def test_backup_returns_backup_path(self):
        """Should return the path where backup was created."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="abc12345def67890  /opt/disco/bin/controller\n")
        mock_ssh.file_exists.return_value = True

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        backup_path = deployer.backup("controller", "/opt/disco/bin/controller")

        assert backup_path is not None
        assert "controller" in backup_path
        assert "/opt/satdeploy/backups" in backup_path
        assert ".bak" in backup_path

    def test_backup_skips_if_file_doesnt_exist(self):
        """Should skip backup if remote file doesn't exist."""
        mock_ssh = Mock()
        mock_ssh.file_exists.return_value = False

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        backup_path = deployer.backup("controller", "/opt/disco/bin/controller")

        assert backup_path is None


class TestDeploy:
    """Test deployment logic."""

    def test_deploy_creates_parent_directory(self, tmp_path):
        """Should create parent directory on remote if needed."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0)

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        deployer.deploy(str(binary), "/opt/disco/bin/controller")

        # Should have called mkdir -p for parent directory
        mkdir_calls = [c for c in mock_ssh.run.call_args_list if "mkdir" in str(c)]
        assert len(mkdir_calls) > 0
        assert "/opt/disco/bin" in str(mkdir_calls[0])

    def test_deploy_uploads_binary(self, tmp_path):
        """Should upload the local binary to remote path."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0)
        mock_ssh.file_exists.return_value = False

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        deployer.deploy(str(binary), "/opt/disco/bin/controller")

        mock_ssh.upload.assert_called_once_with(
            str(binary), "/opt/disco/bin/controller"
        )

    def test_deploy_makes_binary_executable(self, tmp_path):
        """Should chmod +x the deployed binary."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0)
        mock_ssh.file_exists.return_value = False

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        deployer.deploy(str(binary), "/opt/disco/bin/controller")

        chmod_calls = [c for c in mock_ssh.run.call_args_list if "chmod" in str(c)]
        assert len(chmod_calls) > 0
        assert "+x" in str(chmod_calls[-1])


class TestPush:
    """Test the full push workflow."""

    def test_push_returns_deploy_result(self, tmp_path):
        """Push should return a DeployResult."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="active\n", exit_code=0)
        mock_ssh.file_exists.return_value = True

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        result = deployer.push(
            app_name="controller",
            local_path=str(binary),
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        assert isinstance(result, DeployResult)
        assert result.success is True
        assert result.app_name == "controller"

    def test_push_stops_service_before_deploy(self, tmp_path):
        """Push should stop the service before deploying."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="abc12345def67890  /path\n")
        mock_ssh.file_exists.return_value = True

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        deployer.push(
            app_name="controller",
            local_path=str(binary),
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        mock_services.stop.assert_called_with("controller.service")

    def test_push_starts_service_after_deploy(self, tmp_path):
        """Push should start the service after deploying."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="abc12345def67890  /path\n")
        mock_ssh.file_exists.return_value = True

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        deployer.push(
            app_name="controller",
            local_path=str(binary),
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        mock_services.start.assert_called_with("controller.service")

    def test_push_performs_health_check(self, tmp_path):
        """Push should perform a health check after starting service."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="abc12345def67890  /path\n")
        mock_ssh.file_exists.return_value = True

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        result = deployer.push(
            app_name="controller",
            local_path=str(binary),
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        mock_services.is_healthy.assert_called_with("controller.service")
        assert result.health_check_passed is True

    def test_push_result_includes_hash(self, tmp_path):
        """Push result should include the binary hash."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="abc12345def67890  /path\n")
        mock_ssh.file_exists.return_value = True

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        binary = tmp_path / "controller"
        binary.write_bytes(b"binary content")

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        result = deployer.push(
            app_name="controller",
            local_path=str(binary),
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        assert result.binary_hash is not None
        assert len(result.binary_hash) == 8

    def test_push_without_service_skips_service_management(self, tmp_path):
        """Push for library (no service) should skip service management."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="abc12345def67890  /path\n")
        mock_ssh.file_exists.return_value = True

        mock_services = Mock()

        binary = tmp_path / "libparam.so"
        binary.write_bytes(b"library content")

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        result = deployer.push(
            app_name="libparam",
            local_path=str(binary),
            remote_path="/usr/lib/libparam.so",
            service=None,
            service_manager=mock_services,
        )

        mock_services.stop.assert_not_called()
        mock_services.start.assert_not_called()
        assert result.success is True


class TestListBackups:
    """Test listing remote backups."""

    def test_list_backups_returns_list(self):
        """list_backups should return a list of backup info."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n20240114-091500.bak\n",
            exit_code=0,
        )

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        backups = deployer.list_backups("controller")

        assert isinstance(backups, list)
        assert len(backups) == 2

    def test_list_backups_returns_empty_list_when_no_backups(self):
        """list_backups should return empty list if no backups exist."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="", exit_code=0)

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        backups = deployer.list_backups("controller")

        assert backups == []

    def test_list_backups_parses_timestamp(self):
        """list_backups should parse timestamp from backup filename."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        backups = deployer.list_backups("controller")

        assert backups[0]["version"] == "20240115-143022"
        assert backups[0]["timestamp"] == "2024-01-15 14:30:22"

    def test_list_backups_sorted_newest_first(self):
        """list_backups should return backups sorted newest first."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240114-091500.bak\n20240115-143022.bak\n20240113-160000.bak\n",
            exit_code=0,
        )

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        backups = deployer.list_backups("controller")

        assert backups[0]["version"] == "20240115-143022"
        assert backups[1]["version"] == "20240114-091500"
        assert backups[2]["version"] == "20240113-160000"

    def test_list_backups_includes_full_path(self):
        """list_backups should include full path to backup file."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        backups = deployer.list_backups("controller")

        assert backups[0]["path"] == "/opt/satdeploy/backups/controller/20240115-143022.bak"


class TestRollback:
    """Test rollback functionality."""

    def test_rollback_returns_deploy_result(self):
        """rollback should return a DeployResult."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        result = deployer.rollback(
            app_name="controller",
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        assert isinstance(result, DeployResult)

    def test_rollback_restores_most_recent_backup(self):
        """rollback should restore the most recent backup by default."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240114-091500.bak\n20240115-143022.bak\n",
            exit_code=0,
        )

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        deployer.rollback(
            app_name="controller",
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        # Should copy the newest backup (sorted newest first)
        cp_calls = [c for c in mock_ssh.run.call_args_list if "cp" in str(c)]
        assert len(cp_calls) > 0
        assert "20240115-143022.bak" in str(cp_calls[-1])

    def test_rollback_restores_specific_version(self):
        """rollback should restore specific version when provided."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240114-091500.bak\n20240115-143022.bak\n",
            exit_code=0,
        )

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        deployer.rollback(
            app_name="controller",
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
            version="20240114-091500",
        )

        cp_calls = [c for c in mock_ssh.run.call_args_list if "cp" in str(c)]
        assert "20240114-091500.bak" in str(cp_calls[-1])

    def test_rollback_stops_service_before_restore(self):
        """rollback should stop the service before restoring."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        deployer.rollback(
            app_name="controller",
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        mock_services.stop.assert_called_with("controller.service")

    def test_rollback_starts_service_after_restore(self):
        """rollback should start the service after restoring."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        deployer.rollback(
            app_name="controller",
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        mock_services.start.assert_called_with("controller.service")

    def test_rollback_performs_health_check(self):
        """rollback should perform health check after starting service."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        result = deployer.rollback(
            app_name="controller",
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        mock_services.is_healthy.assert_called_with("controller.service")
        assert result.health_check_passed is True

    def test_rollback_fails_when_no_backups(self):
        """rollback should fail if no backups exist."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(stdout="", exit_code=0)

        mock_services = Mock()

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        result = deployer.rollback(
            app_name="controller",
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        assert result.success is False
        assert "no backup" in result.error_message.lower()

    def test_rollback_fails_when_version_not_found(self):
        """rollback should fail if specified version doesn't exist."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        mock_services = Mock()

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        result = deployer.rollback(
            app_name="controller",
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
            version="20240101-000000",
        )

        assert result.success is False
        assert "not found" in result.error_message.lower()

    def test_rollback_result_includes_backup_path(self):
        """rollback result should include the restored backup path."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        mock_services = Mock()
        mock_services.is_healthy.return_value = True

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        result = deployer.rollback(
            app_name="controller",
            remote_path="/opt/disco/bin/controller",
            service="controller.service",
            service_manager=mock_services,
        )

        assert result.backup_path == "/opt/satdeploy/backups/controller/20240115-143022.bak"

    def test_rollback_without_service_skips_service_management(self):
        """rollback for library (no service) should skip service management."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022.bak\n",
            exit_code=0,
        )

        mock_services = Mock()

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        result = deployer.rollback(
            app_name="libparam",
            remote_path="/usr/lib/libparam.so",
            service=None,
            service_manager=mock_services,
        )

        mock_services.stop.assert_not_called()
        mock_services.start.assert_not_called()
        assert result.success is True
