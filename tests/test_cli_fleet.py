"""Tests for the satdeploy fleet commands."""

from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.output import SYMBOLS


class TestFleetCommandGroup:
    """Test the fleet command group exists."""

    def test_fleet_command_exists(self):
        """The fleet command group should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["fleet", "--help"])
        assert result.exit_code == 0
        assert "fleet" in result.output.lower()


class TestFleetStatusCommand:
    """Test the fleet status command."""

    def test_fleet_status_command_exists(self):
        """The fleet status command should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["fleet", "status", "--help"])
        assert result.exit_code == 0

    def test_fleet_status_fails_without_config(self, tmp_path):
        """Fleet status should fail if config doesn't exist."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["fleet", "status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    @patch("satdeploy.cli.SSHClient")
    def test_fleet_status_shows_modules(self, mock_ssh_class, tmp_path):
        """Fleet status should show configured modules."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "modules": {
                        "som1": {
                            "host": "192.168.1.10",
                            "user": "root",
                            "csp_addr": 5421,
                        },
                        "som2": {
                            "host": "192.168.1.11",
                            "user": "root",
                            "csp_addr": 5475,
                        },
                    },
                    "appsys": {
                        "netmask": 8,
                        "interface": 0,
                        "baudrate": 100000,
                        "vmem_path": "/home/root/a53vmem",
                    },
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {},
                }
            )
        )

        # Mock SSH to always succeed
        mock_ssh = MagicMock()
        mock_ssh_class.return_value.__enter__ = Mock(return_value=mock_ssh)
        mock_ssh_class.return_value.__exit__ = Mock(return_value=False)

        result = runner.invoke(
            main,
            ["fleet", "status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "som1" in result.output
        assert "som2" in result.output

    @patch("satdeploy.cli.SSHClient")
    def test_fleet_status_shows_online_offline(self, mock_ssh_class, tmp_path):
        """Fleet status should show online/offline status for modules."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "modules": {
                        "som1": {
                            "host": "192.168.1.10",
                            "user": "root",
                            "csp_addr": 5421,
                        },
                        "som2": {
                            "host": "192.168.1.11",
                            "user": "root",
                            "csp_addr": 5475,
                        },
                    },
                    "appsys": {
                        "netmask": 8,
                        "interface": 0,
                        "baudrate": 100000,
                        "vmem_path": "/home/root/a53vmem",
                    },
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {},
                }
            )
        )

        # Make som1 online and som2 offline
        def ssh_side_effect(host, user, timeout=None):
            mock_ssh = MagicMock()
            if host == "192.168.1.10":
                mock_ssh.__enter__ = Mock(return_value=mock_ssh)
                mock_ssh.__exit__ = Mock(return_value=False)
                return mock_ssh
            else:
                from satdeploy.ssh import SSHError
                raise SSHError("Connection refused")

        mock_ssh_class.side_effect = ssh_side_effect

        result = runner.invoke(
            main,
            ["fleet", "status", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "online" in result.output.lower() or SYMBOLS["check"] in result.output
        assert "offline" in result.output.lower() or SYMBOLS["cross"] in result.output


class TestDiffCommand:
    """Test the diff command."""

    def test_diff_command_exists(self):
        """The diff command should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["diff", "--help"])
        assert result.exit_code == 0
        assert "module" in result.output.lower()

    def test_diff_requires_two_modules(self):
        """Diff should require two module arguments."""
        runner = CliRunner()
        result = runner.invoke(main, ["diff"])
        assert result.exit_code != 0

    def test_diff_fails_without_config(self, tmp_path):
        """Diff should fail if config doesn't exist."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["diff", "som1", "som2", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    def test_diff_shows_differences(self, tmp_path):
        """Diff should show matching and differing apps."""
        from satdeploy.history import History, DeploymentRecord

        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "modules": {
                        "som1": {
                            "host": "192.168.1.10",
                            "user": "root",
                            "csp_addr": 5421,
                        },
                        "som2": {
                            "host": "192.168.1.11",
                            "user": "root",
                            "csp_addr": 5475,
                        },
                    },
                    "appsys": {},
                    "backup_dir": "/opt/satdeploy/backups",
                    "apps": {
                        "app1": {"local": "./app1", "remote": "/usr/bin/app1"},
                        "app2": {"local": "./app2", "remote": "/usr/bin/app2"},
                    },
                }
            )
        )

        # Create deployment history
        history = History(config_dir / "history.db")
        history.init_db()

        # app1 has same hash on both modules
        history.record(DeploymentRecord(
            module="som1",
            app="app1",
            binary_hash="abc12345",
            remote_path="/usr/bin/app1",
            action="push",
            success=True,
        ))
        history.record(DeploymentRecord(
            module="som2",
            app="app1",
            binary_hash="abc12345",
            remote_path="/usr/bin/app1",
            action="push",
            success=True,
        ))

        # app2 has different hash on each module
        history.record(DeploymentRecord(
            module="som1",
            app="app2",
            binary_hash="def67890",
            remote_path="/usr/bin/app2",
            action="push",
            success=True,
        ))
        history.record(DeploymentRecord(
            module="som2",
            app="app2",
            binary_hash="xyz99999",
            remote_path="/usr/bin/app2",
            action="push",
            success=True,
        ))

        result = runner.invoke(
            main,
            ["diff", "som1", "som2", "--config-dir", str(config_dir)],
        )

        assert result.exit_code == 0
        assert "app1" in result.output
        assert "app2" in result.output
        assert "match" in result.output.lower()
        assert "differs" in result.output.lower()


class TestSyncCommand:
    """Test the sync command."""

    def test_sync_command_exists(self):
        """The sync command should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["sync", "--help"])
        assert result.exit_code == 0
        assert "source" in result.output.lower()
        assert "target" in result.output.lower()

    def test_sync_requires_two_modules(self):
        """Sync should require source and target arguments."""
        runner = CliRunner()
        result = runner.invoke(main, ["sync"])
        assert result.exit_code != 0

    def test_sync_fails_without_config(self, tmp_path):
        """Sync should fail if config doesn't exist."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["sync", "som1", "som2", "--config-dir", str(config_dir)],
        )

        assert result.exit_code != 0
        assert "config" in result.output.lower()

    def test_sync_has_clean_vmem_option(self):
        """Sync should have --clean-vmem option."""
        runner = CliRunner()
        result = runner.invoke(main, ["sync", "--help"])
        assert result.exit_code == 0
        assert "--clean-vmem" in result.output

    def test_sync_has_yes_option(self):
        """Sync should have --yes option to skip confirmation."""
        runner = CliRunner()
        result = runner.invoke(main, ["sync", "--help"])
        assert result.exit_code == 0
        assert "--yes" in result.output or "-y" in result.output


class TestPushMultiModuleCommand:
    """Test the push command with multi-module support."""

    def test_push_has_module_option(self):
        """Push should have --module option."""
        runner = CliRunner()
        result = runner.invoke(main, ["push", "--help"])
        assert result.exit_code == 0
        assert "--module" in result.output or "-m" in result.output

    def test_push_has_all_option(self):
        """Push should have --all option."""
        runner = CliRunner()
        result = runner.invoke(main, ["push", "--help"])
        assert result.exit_code == 0
        assert "--all" in result.output

    def test_push_has_clean_vmem_option(self):
        """Push should have --clean-vmem option."""
        runner = CliRunner()
        result = runner.invoke(main, ["push", "--help"])
        assert result.exit_code == 0
        assert "--clean-vmem" in result.output
