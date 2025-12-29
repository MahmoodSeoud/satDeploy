"""Tests for deployer module."""

from unittest.mock import Mock

import pytest

from satdeploy.deployer import Deployer


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


class TestListBackups:
    """Test listing remote backups."""

    def test_list_backups_returns_list(self):
        """list_backups should return a list of backup info."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n20240114-091500-def67890.bak\n",
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
            stdout="20240115-143022-abc12345.bak\n",
            exit_code=0,
        )

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        backups = deployer.list_backups("controller")

        assert backups[0]["version"] == "20240115-143022-abc12345"
        assert backups[0]["timestamp"] == "2024-01-15 14:30:22"
        assert backups[0]["hash"] == "abc12345"

    def test_list_backups_sorted_newest_first(self):
        """list_backups should return backups sorted newest first."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240114-091500-def67890.bak\n20240115-143022-abc12345.bak\n20240113-160000-11223344.bak\n",
            exit_code=0,
        )

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        backups = deployer.list_backups("controller")

        assert backups[0]["version"] == "20240115-143022-abc12345"
        assert backups[1]["version"] == "20240114-091500-def67890"
        assert backups[2]["version"] == "20240113-160000-11223344"

    def test_list_backups_includes_full_path(self):
        """list_backups should include full path to backup file."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(
            stdout="20240115-143022-abc12345.bak\n",
            exit_code=0,
        )

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        backups = deployer.list_backups("controller")

        assert backups[0]["path"] == "/opt/satdeploy/backups/controller/20240115-143022-abc12345.bak"


class TestClearVmemDir:
    """Test vmem directory clearing."""

    def test_clear_vmem_dir_removes_contents_and_recreates(self):
        """Should remove vmem directory contents and recreate empty directory."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0)

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )
        deployer.clear_vmem_dir("/home/root/a53vmem")

        # Should call rm -rf to clear and mkdir to recreate
        run_calls = [str(c) for c in mock_ssh.run.call_args_list]
        assert any("rm -rf" in call and "/home/root/a53vmem" in call for call in run_calls)
        assert any("mkdir -p" in call and "/home/root/a53vmem" in call for call in run_calls)


class TestWriteRemoteFile:
    """Test writing string content to remote file."""

    def test_write_remote_file_creates_file_with_content(self):
        """Should write string content to a remote file via shell."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0)

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )

        content = "[Unit]\nDescription=Test Service\n"
        deployer.write_remote_file("/etc/systemd/system/test.service", content)

        # Should have called run with content being written to file
        run_calls = [str(c) for c in mock_ssh.run.call_args_list]
        assert len(run_calls) == 1
        assert "/etc/systemd/system/test.service" in run_calls[0]
        # Verify the content is passed somehow (tee, cat heredoc, etc.)
        assert "Test Service" in run_calls[0]


class TestUploadService:
    """Test uploading service files with systemd reload."""

    def test_upload_service_writes_file_and_reloads_systemd(self):
        """Should write service file and reload systemd daemon."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0)

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )

        content = "[Unit]\nDescription=Test Service\n[Service]\nExecStart=/usr/bin/test\n"
        deployer.upload_service("test.service", content)

        run_calls = [str(c) for c in mock_ssh.run.call_args_list]
        # Should write to /etc/systemd/system/
        assert any("/etc/systemd/system/test.service" in call for call in run_calls)
        # Should call systemctl daemon-reload
        assert any("systemctl daemon-reload" in call for call in run_calls)

    def test_upload_service_writes_content_correctly(self):
        """Should write the service content to the correct path."""
        mock_ssh = Mock()
        mock_ssh.run.return_value = Mock(exit_code=0)

        deployer = Deployer(
            ssh=mock_ssh,
            backup_dir="/opt/satdeploy/backups",
            max_backups=10,
        )

        content = "[Unit]\nDescription=A53 Manager\n"
        deployer.upload_service("a53-manager.service", content)

        # First call should be the write_remote_file
        first_call = str(mock_ssh.run.call_args_list[0])
        assert "/etc/systemd/system/a53-manager.service" in first_call
        assert "A53 Manager" in first_call
