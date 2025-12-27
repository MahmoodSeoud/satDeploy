"""Integration tests for push-list workflow.

These tests verify that after pushing twice, the list command shows
the backup from the first version.
"""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main


class TestPushThenListWorkflow:
    """Test the push-push-list workflow that should show backups."""

    @patch("satdeploy.cli.SSHClient")
    def test_push_twice_then_list_shows_one_backup(self, mock_ssh_class, tmp_path):
        """After pushing twice from clean state, list should show one backup.

        Scenario:
        1. Clean slate (no backups, no remote binary)
        2. Push v1 - no backup created (nothing to back up)
        3. Push v2 - v1 is backed up
        4. List should show 1 backup (v1)
        """
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        # Create local binaries
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        binary_v1 = build_dir / "test_app"
        binary_v1.write_bytes(b"version 1 content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/home/user/.satdeploy/backups",
                    "max_backups": 10,
                    "apps": {
                        "test_app": {
                            "local": str(binary_v1),
                            "remote": "/home/user/bin/test_app",
                            "service": None,
                        }
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)

        # First push: remote file doesn't exist
        mock_ssh.file_exists.return_value = False
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="")

        result1 = runner.invoke(
            main,
            ["push", "test_app", "--config-dir", str(config_dir)],
        )
        assert result1.exit_code == 0, f"First push failed: {result1.output}"

        # Now modify binary for second push
        binary_v1.write_bytes(b"version 2 content - different!")

        # Second push: remote file NOW exists, and we need to return hash
        mock_ssh.file_exists.return_value = True
        # Return a hash when sha256sum is called (for backup naming)
        mock_ssh.run.side_effect = lambda cmd, **kwargs: Mock(
            exit_code=0,
            stdout="abc12345def67890  /home/user/bin/test_app\n" if "sha256sum" in cmd else ""
        )

        result2 = runner.invoke(
            main,
            ["push", "test_app", "--config-dir", str(config_dir)],
        )
        assert result2.exit_code == 0, f"Second push failed: {result2.output}"

        # Now list should show the backup
        # The backup was created during second push with timestamp-hash format
        mock_ssh.run.side_effect = None
        mock_ssh.run.return_value = Mock(
            exit_code=0,
            # This simulates what ls would return on the backup directory
            stdout="20241227-120000-abc12345.bak\n"
        )

        result3 = runner.invoke(
            main,
            ["list", "test_app", "--config-dir", str(config_dir)],
        )

        assert result3.exit_code == 0, f"List failed: {result3.output}"
        # Should show the backup with timestamp and hash
        assert "abc12345" in result3.output or "2024-12-27" in result3.output, \
            f"Expected backup info in output but got: {result3.output}"
        # Should NOT say "no backups"
        assert "no backup" not in result3.output.lower(), \
            f"Unexpected 'no backups' message: {result3.output}"

    @patch("satdeploy.cli.SSHClient")
    def test_list_shows_empty_when_no_versions_exist(self, mock_ssh_class, tmp_path):
        """List should show 'no versions' message when nothing deployed and no backups."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        binary = build_dir / "test_app"
        binary.write_bytes(b"content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/home/user/.satdeploy/backups",
                    "max_backups": 10,
                    "apps": {
                        "test_app": {
                            "local": str(binary),
                            "remote": "/home/user/bin/test_app",
                            "service": None,
                        }
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)

        # Empty backup directory
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="")

        result = runner.invoke(
            main,
            ["list", "test_app", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "no versions" in result.output.lower()


class TestListShowsCurrentlyDeployed:
    """Test that list shows both currently deployed version and backups."""

    @patch("satdeploy.cli.SSHClient")
    def test_list_shows_currently_deployed_version(self, mock_ssh_class, tmp_path):
        """List should show currently deployed version marked as 'current'.

        After 2 pushes:
        - v1 is backed up
        - v2 is currently deployed
        List should show BOTH versions.
        """
        from satdeploy.history import History, DeploymentRecord

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        binary = build_dir / "test_app"
        binary.write_bytes(b"content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/home/user/.satdeploy/backups",
                    "max_backups": 10,
                    "apps": {
                        "test_app": {
                            "local": str(binary),
                            "remote": "/home/user/bin/test_app",
                            "service": None,
                        }
                    },
                }
            )
        )

        # Set up history with 2 pushes
        history = History(config_dir / "history.db")
        history.init_db()
        # First push - no backup
        history.record(DeploymentRecord(
            app="test_app",
            binary_hash="aaaaaaaa",
            remote_path="/home/user/bin/test_app",
            backup_path=None,
            action="push",
            success=True,
        ))
        # Second push - v1 backed up
        history.record(DeploymentRecord(
            app="test_app",
            binary_hash="bbbbbbbb",
            remote_path="/home/user/bin/test_app",
            backup_path="/home/user/.satdeploy/backups/test_app/20241227-120000-aaaaaaaa.bak",
            action="push",
            success=True,
        ))

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)

        # Remote file exists
        mock_ssh.file_exists.return_value = True

        # Backup directory has v1 backup
        mock_ssh.run.return_value = Mock(
            exit_code=0,
            stdout="20241227-120000-aaaaaaaa.bak\n"
        )

        result = runner.invoke(
            main,
            ["list", "test_app", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0, f"List failed: {result.output}"

        # Should show currently deployed version (bbbbbbbb)
        assert "bbbbbbbb" in result.output, \
            f"Expected currently deployed hash bbbbbbbb in output: {result.output}"

        # Should show backup version (aaaaaaaa)
        assert "aaaaaaaa" in result.output, \
            f"Expected backup hash aaaaaaaa in output: {result.output}"

        # Should indicate which is current
        assert "current" in result.output.lower() or "deployed" in result.output.lower(), \
            f"Expected 'current' or 'deployed' marker in output: {result.output}"

    @patch("satdeploy.cli.SSHClient")
    def test_list_shows_deployed_even_with_no_backups(self, mock_ssh_class, tmp_path):
        """List should show currently deployed version even when no backups exist.

        After first push (no backups), list should still show the deployed version.
        """
        from satdeploy.history import History, DeploymentRecord

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        binary = build_dir / "test_app"
        binary.write_bytes(b"content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/home/user/.satdeploy/backups",
                    "max_backups": 10,
                    "apps": {
                        "test_app": {
                            "local": str(binary),
                            "remote": "/home/user/bin/test_app",
                            "service": None,
                        }
                    },
                }
            )
        )

        # Set up history with 1 push (no backup)
        history = History(config_dir / "history.db")
        history.init_db()
        history.record(DeploymentRecord(
            app="test_app",
            binary_hash="aaaaaaaa",
            remote_path="/home/user/bin/test_app",
            backup_path=None,
            action="push",
            success=True,
        ))

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)

        # Remote file exists
        mock_ssh.file_exists.return_value = True

        # No backups
        mock_ssh.run.return_value = Mock(exit_code=0, stdout="")

        result = runner.invoke(
            main,
            ["list", "test_app", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0, f"List failed: {result.output}"

        # Should show currently deployed version
        assert "aaaaaaaa" in result.output, \
            f"Expected deployed hash aaaaaaaa in output: {result.output}"

        # Should NOT say "no backups found" anymore since we show the current version
        # (Or we could show both - current version + "no backups")


class TestBackupCreatedOnSecondPush:
    """Test that backup is actually created when pushing over existing file."""

    @patch("satdeploy.cli.SSHClient")
    def test_second_push_creates_backup_with_hash_in_filename(self, mock_ssh_class, tmp_path):
        """When pushing over existing binary, backup filename includes hash."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()

        build_dir = tmp_path / "build"
        build_dir.mkdir()
        binary = build_dir / "test_app"
        binary.write_bytes(b"content")

        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "target": {"host": "192.168.1.50", "user": "root"},
                    "backup_dir": "/home/user/.satdeploy/backups",
                    "max_backups": 10,
                    "apps": {
                        "test_app": {
                            "local": str(binary),
                            "remote": "/home/user/bin/test_app",
                            "service": None,
                        }
                    },
                }
            )
        )

        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)

        # Remote file exists
        mock_ssh.file_exists.return_value = True

        # Track all commands run
        commands_run = []
        def track_run(cmd, **kwargs):
            commands_run.append(cmd)
            if "sha256sum" in cmd:
                return Mock(exit_code=0, stdout="abc12345def67890  /path\n")
            return Mock(exit_code=0, stdout="")

        mock_ssh.run.side_effect = track_run

        result = runner.invoke(
            main,
            ["push", "test_app", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0, f"Push failed: {result.output}"

        # Verify backup was created with hash in filename
        cp_commands = [c for c in commands_run if c.startswith("cp ")]
        assert len(cp_commands) >= 1, f"No cp command found. Commands: {commands_run}"

        # The backup should include the hash
        backup_cp = cp_commands[0]
        assert "abc12345" in backup_cp, \
            f"Expected hash in backup filename, got: {backup_cp}"
        assert ".bak" in backup_cp, \
            f"Expected .bak extension, got: {backup_cp}"
