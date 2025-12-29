"""Tests for SSH module."""

import pytest
from unittest.mock import Mock, MagicMock, patch

from satdeploy.ssh import SSHClient, SSHError


class TestSSHClient:
    """Test SSHClient class."""

    def test_connect_uses_host_and_user(self):
        """Connect should use the provided host and user."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()

            mock_ssh.return_value.connect.assert_called_once()
            call_kwargs = mock_ssh.return_value.connect.call_args[1]
            assert call_kwargs["hostname"] == "192.168.1.50"
            assert call_kwargs["username"] == "root"

    def test_connect_loads_system_host_keys(self):
        """Connect should load system host keys."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()

            mock_ssh.return_value.load_system_host_keys.assert_called_once()

    def test_connect_sets_missing_host_key_policy(self):
        """Connect should set a policy for unknown hosts."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()

            mock_ssh.return_value.set_missing_host_key_policy.assert_called_once()

    def test_disconnect_closes_connection(self):
        """Disconnect should close the SSH connection."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            client.disconnect()

            mock_ssh.return_value.close.assert_called_once()

    def test_run_command_returns_stdout(self):
        """Run command should return stdout content."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_stdout = Mock()
            mock_stdout.read.return_value = b"output"
            mock_stdout.channel.recv_exit_status.return_value = 0
            mock_stderr = Mock()
            mock_stderr.read.return_value = b""
            mock_ssh.return_value.exec_command.return_value = (
                Mock(),
                mock_stdout,
                mock_stderr,
            )

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            result = client.run("ls -la")

            assert result.stdout == "output"
            assert result.exit_code == 0

    def test_run_command_raises_on_failure(self):
        """Run command should raise SSHError on non-zero exit."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_stdout = Mock()
            mock_stdout.read.return_value = b""
            mock_stdout.channel.recv_exit_status.return_value = 1
            mock_stderr = Mock()
            mock_stderr.read.return_value = b"error message"
            mock_ssh.return_value.exec_command.return_value = (
                Mock(),
                mock_stdout,
                mock_stderr,
            )

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()

            with pytest.raises(SSHError) as exc_info:
                client.run("failing command")
            assert "error message" in str(exc_info.value)

    def test_run_command_can_ignore_errors(self):
        """Run command should not raise if check=False."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_stdout = Mock()
            mock_stdout.read.return_value = b""
            mock_stdout.channel.recv_exit_status.return_value = 1
            mock_stderr = Mock()
            mock_stderr.read.return_value = b"error"
            mock_ssh.return_value.exec_command.return_value = (
                Mock(),
                mock_stdout,
                mock_stderr,
            )

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            result = client.run("cmd", check=False)

            assert result.exit_code == 1

    def test_context_manager_connects_and_disconnects(self):
        """SSHClient should work as a context manager."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            with SSHClient(host="192.168.1.50", user="root") as client:
                pass

            mock_ssh.return_value.connect.assert_called_once()
            mock_ssh.return_value.close.assert_called_once()


class TestSSHFileOperations:
    """Test SSH file operations."""

    def test_upload_file(self):
        """Should upload a local file to remote path."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_sftp = MagicMock()
            mock_ssh.return_value.open_sftp.return_value = mock_sftp

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            client.upload("/local/file", "/remote/file")

            mock_sftp.put.assert_called_once_with("/local/file", "/remote/file")

    def test_download_file(self):
        """Should download a remote file to local path."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_sftp = MagicMock()
            mock_ssh.return_value.open_sftp.return_value = mock_sftp

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            client.download("/remote/file", "/local/file")

            mock_sftp.get.assert_called_once_with("/remote/file", "/local/file")

    def test_file_exists_returns_true(self):
        """Should return True if remote file exists."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_sftp = MagicMock()
            mock_ssh.return_value.open_sftp.return_value = mock_sftp

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            result = client.file_exists("/remote/file")

            assert result is True
            mock_sftp.stat.assert_called_once_with("/remote/file")

    def test_file_exists_returns_false(self):
        """Should return False if remote file doesn't exist."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_sftp = MagicMock()
            mock_sftp.stat.side_effect = FileNotFoundError()
            mock_ssh.return_value.open_sftp.return_value = mock_sftp

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            result = client.file_exists("/remote/file")

            assert result is False

    def test_copy_remote_file(self):
        """Should copy a file on the remote system."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_stdout = Mock()
            mock_stdout.read.return_value = b""
            mock_stdout.channel.recv_exit_status.return_value = 0
            mock_stderr = Mock()
            mock_stderr.read.return_value = b""
            mock_ssh.return_value.exec_command.return_value = (
                Mock(),
                mock_stdout,
                mock_stderr,
            )

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            client.copy_remote("/src/file", "/dst/file")

            mock_ssh.return_value.exec_command.assert_called()
            cmd = mock_ssh.return_value.exec_command.call_args[0][0]
            assert "cp" in cmd
            assert "/src/file" in cmd
            assert "/dst/file" in cmd


class TestSSHConnectionErrors:
    """Test SSH connection error handling."""

    def test_connection_refused_raises_ssh_error(self):
        """Connection refused should raise SSHError with helpful message."""
        import socket

        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_ssh.return_value.connect.side_effect = socket.error(
                "[Errno 61] Connection refused"
            )

            client = SSHClient(host="192.168.1.50", user="root")
            with pytest.raises(SSHError) as exc_info:
                client.connect()

            assert "192.168.1.50" in str(exc_info.value)
            assert "connection" in str(exc_info.value).lower()

    def test_authentication_failed_raises_ssh_error(self):
        """Authentication failure should raise SSHError with helpful message."""
        import paramiko

        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_ssh.return_value.connect.side_effect = paramiko.AuthenticationException(
                "Authentication failed"
            )

            client = SSHClient(host="192.168.1.50", user="root")
            with pytest.raises(SSHError) as exc_info:
                client.connect()

            assert "authentication" in str(exc_info.value).lower()

    def test_host_unreachable_raises_ssh_error(self):
        """Unreachable host should raise SSHError with helpful message."""
        import socket

        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_ssh.return_value.connect.side_effect = socket.timeout(
                "Connection timed out"
            )

            client = SSHClient(host="192.168.1.50", user="root")
            with pytest.raises(SSHError) as exc_info:
                client.connect()

            assert "192.168.1.50" in str(exc_info.value) or "timeout" in str(exc_info.value).lower()

    def test_host_key_mismatch_raises_ssh_error(self):
        """Host key verification failure should raise SSHError."""
        import paramiko

        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_ssh.return_value.connect.side_effect = paramiko.SSHException(
                "Host key verification failed"
            )

            client = SSHClient(host="192.168.1.50", user="root")
            with pytest.raises(SSHError) as exc_info:
                client.connect()

            assert "192.168.1.50" in str(exc_info.value) or "ssh" in str(exc_info.value).lower()


class TestSSHReadWriteFile:
    """Test SSH file read/write operations."""

    def test_read_file_returns_content(self):
        """Should return file content as string."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_stdout = Mock()
            mock_stdout.read.return_value = b"file content"
            mock_stdout.channel.recv_exit_status.return_value = 0
            mock_stderr = Mock()
            mock_stderr.read.return_value = b""
            mock_ssh.return_value.exec_command.return_value = (
                Mock(),
                mock_stdout,
                mock_stderr,
            )

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            result = client.read_file("/etc/systemd/system/test.service")

            assert result == "file content"
            cmd = mock_ssh.return_value.exec_command.call_args[0][0]
            assert "cat" in cmd
            assert "/etc/systemd/system/test.service" in cmd

    def test_read_file_returns_none_if_not_exists(self):
        """Should return None if file doesn't exist."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_stdout = Mock()
            mock_stdout.read.return_value = b""
            mock_stdout.channel.recv_exit_status.return_value = 1
            mock_stderr = Mock()
            mock_stderr.read.return_value = b"No such file"
            mock_ssh.return_value.exec_command.return_value = (
                Mock(),
                mock_stdout,
                mock_stderr,
            )

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            result = client.read_file("/nonexistent/file")

            assert result is None

    def test_write_file_sudo_uses_tee(self):
        """Should use sudo tee to write file."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_stdout = Mock()
            mock_stdout.read.return_value = b""
            mock_stdout.channel.recv_exit_status.return_value = 0
            mock_stderr = Mock()
            mock_stderr.read.return_value = b""
            mock_ssh.return_value.exec_command.return_value = (
                Mock(),
                mock_stdout,
                mock_stderr,
            )

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            client.write_file_sudo("/etc/systemd/system/test.service", "[Service]\nExecStart=/bin/app")

            cmd = mock_ssh.return_value.exec_command.call_args[0][0]
            assert "sudo tee" in cmd
            assert "/etc/systemd/system/test.service" in cmd

    def test_write_file_sudo_escapes_quotes(self):
        """Should escape single quotes in content."""
        with patch("satdeploy.ssh.paramiko.SSHClient") as mock_ssh:
            mock_stdout = Mock()
            mock_stdout.read.return_value = b""
            mock_stdout.channel.recv_exit_status.return_value = 0
            mock_stderr = Mock()
            mock_stderr.read.return_value = b""
            mock_ssh.return_value.exec_command.return_value = (
                Mock(),
                mock_stdout,
                mock_stderr,
            )

            client = SSHClient(host="192.168.1.50", user="root")
            client.connect()
            client.write_file_sudo("/test", "it's a test")

            cmd = mock_ssh.return_value.exec_command.call_args[0][0]
            # Single quote should be escaped
            assert "'" in cmd
