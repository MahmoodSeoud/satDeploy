"""Tests for CLI transport integration."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from satdeploy.config import ModuleConfig, Config
from satdeploy.transport import SSHTransport, CSPTransport
from satdeploy.transport.base import DeployResult
from satdeploy.cli import get_transport, main


class TestGetTransport:
    """Test transport factory function."""

    def test_get_transport_returns_ssh_for_ssh_module(self):
        """get_transport returns SSHTransport for SSH modules."""
        module = ModuleConfig(
            name="som1",
            transport="ssh",
            host="192.168.1.10",
            user="root",
        )
        backup_dir = "/opt/satdeploy/backups"

        transport = get_transport(module, backup_dir)

        assert isinstance(transport, SSHTransport)
        assert transport.host == "192.168.1.10"
        assert transport.user == "root"

    def test_get_transport_returns_csp_for_csp_module(self):
        """get_transport returns CSPTransport for CSP modules."""
        module = ModuleConfig(
            name="som1-csp",
            transport="csp",
            zmq_endpoint="tcp://localhost:4040",
            agent_node=5424,
            ground_node=4040,
        )
        backup_dir = "/opt/satdeploy/backups"

        transport = get_transport(module, backup_dir)

        assert isinstance(transport, CSPTransport)
        assert transport.zmq_endpoint == "tcp://localhost:4040"
        assert transport.agent_node == 5424

    def test_get_transport_raises_for_unknown_transport(self):
        """get_transport raises error for unknown transport type."""
        module = ModuleConfig(
            name="som1",
            transport="unknown",
            host="192.168.1.10",
            user="root",
        )
        backup_dir = "/opt/satdeploy/backups"

        with pytest.raises(ValueError, match="Unknown transport"):
            get_transport(module, backup_dir)


class TestPushWithCSPTransport:
    """Test push command with CSP transport."""

    @pytest.fixture
    def config_dir(self, tmp_path):
        """Create a temporary config directory."""
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        return config_dir

    @pytest.fixture
    def local_binary(self, tmp_path):
        """Create a temporary local binary."""
        binary = tmp_path / "test_binary"
        binary.write_bytes(b"binary content here")
        return binary

    @pytest.fixture
    def csp_config(self, config_dir, local_binary):
        """Create config with CSP module."""
        config_yaml = config_dir / "config.yaml"
        config_yaml.write_text(f"""
modules:
  sat1:
    transport: csp
    zmq_endpoint: tcp://localhost:4040
    agent_node: 5424
    ground_node: 4040
    appsys_node: 10
    app_nodes:
      dipp: 5

backup_dir: /opt/satdeploy/backups
max_backups: 10

apps:
  dipp:
    local: {local_binary}
    remote: /opt/disco/bin/dipp
    param: mng_dipp
""")
        return config_dir

    def test_push_csp_calls_transport_deploy(self, csp_config, local_binary):
        """Push with CSP module uses CSPTransport.deploy()."""
        runner = CliRunner()
        mock_transport = MagicMock()
        mock_transport.deploy.return_value = DeployResult(
            success=True,
            backup_path="/opt/satdeploy/backups/dipp/20250131-120000-abc123.bak",
        )

        with patch("satdeploy.cli.get_transport", return_value=mock_transport):
            result = runner.invoke(
                main,
                ["push", "dipp", "-m", "sat1", "--config-dir", str(csp_config)],
            )

        assert result.exit_code == 0, f"Output: {result.output}"
        mock_transport.connect.assert_called_once()
        mock_transport.deploy.assert_called_once()
        mock_transport.disconnect.assert_called_once()

        # Check deploy was called with correct args
        call_args = mock_transport.deploy.call_args
        assert call_args.kwargs["app_name"] == "dipp"
        assert call_args.kwargs["remote_path"] == "/opt/disco/bin/dipp"

    def test_push_csp_records_history_on_success(self, csp_config, local_binary):
        """Push with CSP records deployment history."""
        runner = CliRunner()
        mock_transport = MagicMock()
        mock_transport.deploy.return_value = DeployResult(
            success=True,
            backup_path="/opt/satdeploy/backups/dipp/backup.bak",
        )

        with patch("satdeploy.cli.get_transport", return_value=mock_transport):
            result = runner.invoke(
                main,
                ["push", "dipp", "-m", "sat1", "--config-dir", str(csp_config)],
            )

        assert result.exit_code == 0

    def test_push_csp_reports_error_on_failure(self, csp_config, local_binary):
        """Push with CSP reports error when deploy fails."""
        runner = CliRunner()
        mock_transport = MagicMock()
        mock_transport.deploy.return_value = DeployResult(
            success=False,
            error_message="DTP download failed",
            error_code=5,
        )

        with patch("satdeploy.cli.get_transport", return_value=mock_transport):
            result = runner.invoke(
                main,
                ["push", "dipp", "-m", "sat1", "--config-dir", str(csp_config)],
            )

        assert result.exit_code == 1
        assert "DTP download failed" in result.output


class TestRollbackWithCSPTransport:
    """Test rollback command with CSP transport."""

    @pytest.fixture
    def config_dir(self, tmp_path):
        """Create a temporary config directory."""
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        return config_dir

    @pytest.fixture
    def local_binary(self, tmp_path):
        """Create a temporary local binary."""
        binary = tmp_path / "test_binary"
        binary.write_bytes(b"binary content here")
        return binary

    @pytest.fixture
    def csp_config(self, config_dir, local_binary):
        """Create config with CSP module."""
        config_yaml = config_dir / "config.yaml"
        config_yaml.write_text(f"""
modules:
  sat1:
    transport: csp
    zmq_endpoint: tcp://localhost:4040
    agent_node: 5424
    ground_node: 4040
    appsys_node: 10

backup_dir: /opt/satdeploy/backups
max_backups: 10

apps:
  dipp:
    local: {local_binary}
    remote: /opt/disco/bin/dipp
    param: mng_dipp
""")
        return config_dir

    def test_rollback_csp_calls_transport_rollback(self, csp_config, local_binary):
        """Rollback with CSP module uses CSPTransport.rollback()."""
        runner = CliRunner()
        mock_transport = MagicMock()
        mock_transport.rollback.return_value = DeployResult(success=True)

        with patch("satdeploy.cli.get_transport", return_value=mock_transport):
            result = runner.invoke(
                main,
                ["rollback", "dipp", "-m", "sat1", "--config-dir", str(csp_config)],
            )

        assert result.exit_code == 0, f"Output: {result.output}"
        mock_transport.connect.assert_called_once()
        mock_transport.rollback.assert_called_once()
        mock_transport.disconnect.assert_called_once()

        # Check rollback was called with correct args
        call_args = mock_transport.rollback.call_args
        assert call_args.kwargs["app_name"] == "dipp"

    def test_rollback_csp_with_specific_hash(self, csp_config, local_binary):
        """Rollback with CSP passes specific hash to transport."""
        runner = CliRunner()
        mock_transport = MagicMock()
        mock_transport.rollback.return_value = DeployResult(success=True)

        with patch("satdeploy.cli.get_transport", return_value=mock_transport):
            result = runner.invoke(
                main,
                ["rollback", "dipp", "abc12345", "-m", "sat1", "--config-dir", str(csp_config)],
            )

        assert result.exit_code == 0, f"Output: {result.output}"
        call_args = mock_transport.rollback.call_args
        assert call_args.kwargs["backup_hash"] == "abc12345"

    def test_rollback_csp_reports_error_on_failure(self, csp_config, local_binary):
        """Rollback with CSP reports error when it fails."""
        runner = CliRunner()
        mock_transport = MagicMock()
        mock_transport.rollback.return_value = DeployResult(
            success=False,
            error_message="No backups available",
            error_code=8,
        )

        with patch("satdeploy.cli.get_transport", return_value=mock_transport):
            result = runner.invoke(
                main,
                ["rollback", "dipp", "-m", "sat1", "--config-dir", str(csp_config)],
            )

        assert result.exit_code == 1
        assert "No backups available" in result.output


class TestStatusWithCSPTransport:
    """Test status command with CSP transport."""

    @pytest.fixture
    def config_dir(self, tmp_path):
        """Create a temporary config directory."""
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        return config_dir

    @pytest.fixture
    def local_binary(self, tmp_path):
        """Create a temporary local binary."""
        binary = tmp_path / "test_binary"
        binary.write_bytes(b"binary content here")
        return binary

    @pytest.fixture
    def csp_config(self, config_dir, local_binary):
        """Create config with CSP module."""
        config_yaml = config_dir / "config.yaml"
        config_yaml.write_text(f"""
modules:
  sat1:
    transport: csp
    zmq_endpoint: tcp://localhost:4040
    agent_node: 5424
    ground_node: 4040
    appsys_node: 10

backup_dir: /opt/satdeploy/backups
max_backups: 10

apps:
  dipp:
    local: {local_binary}
    remote: /opt/disco/bin/dipp
    param: mng_dipp
""")
        return config_dir

    def test_status_csp_calls_transport_get_status(self, csp_config, local_binary):
        """Status with CSP module uses CSPTransport.get_status()."""
        from satdeploy.transport.base import AppStatus

        runner = CliRunner()
        mock_transport = MagicMock()
        mock_transport.get_status.return_value = {
            "dipp": AppStatus(
                app_name="dipp",
                running=True,
                binary_hash="abc12345",
                remote_path="/opt/disco/bin/dipp",
            )
        }

        with patch("satdeploy.cli.get_transport", return_value=mock_transport):
            result = runner.invoke(
                main,
                ["status", "-m", "sat1", "--config-dir", str(csp_config)],
            )

        assert result.exit_code == 0, f"Output: {result.output}"
        mock_transport.connect.assert_called_once()
        mock_transport.get_status.assert_called_once()
        mock_transport.disconnect.assert_called_once()
        assert "dipp" in result.output
        assert "running" in result.output


class TestListWithCSPTransport:
    """Test list command with CSP transport."""

    @pytest.fixture
    def config_dir(self, tmp_path):
        """Create a temporary config directory."""
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        return config_dir

    @pytest.fixture
    def local_binary(self, tmp_path):
        """Create a temporary local binary."""
        binary = tmp_path / "test_binary"
        binary.write_bytes(b"binary content here")
        return binary

    @pytest.fixture
    def csp_config(self, config_dir, local_binary):
        """Create config with CSP module."""
        config_yaml = config_dir / "config.yaml"
        config_yaml.write_text(f"""
modules:
  sat1:
    transport: csp
    zmq_endpoint: tcp://localhost:4040
    agent_node: 5424
    ground_node: 4040
    appsys_node: 10

backup_dir: /opt/satdeploy/backups
max_backups: 10

apps:
  dipp:
    local: {local_binary}
    remote: /opt/disco/bin/dipp
    param: mng_dipp
""")
        return config_dir

    def test_list_csp_calls_transport_list_backups(self, csp_config, local_binary):
        """List with CSP module uses CSPTransport.list_backups()."""
        from satdeploy.transport.base import BackupInfo

        runner = CliRunner()
        mock_transport = MagicMock()
        mock_transport.list_backups.return_value = [
            BackupInfo(
                version="20250131-120000",
                timestamp="2025-01-31 12:00:00",
                binary_hash="abc12345",
                path="/opt/satdeploy/backups/dipp/20250131-120000-abc12345.bak",
            ),
            BackupInfo(
                version="20250130-100000",
                timestamp="2025-01-30 10:00:00",
                binary_hash="def67890",
                path="/opt/satdeploy/backups/dipp/20250130-100000-def67890.bak",
            ),
        ]

        with patch("satdeploy.cli.get_transport", return_value=mock_transport):
            result = runner.invoke(
                main,
                ["list", "dipp", "-m", "sat1", "--config-dir", str(csp_config)],
            )

        assert result.exit_code == 0, f"Output: {result.output}"
        mock_transport.connect.assert_called_once()
        mock_transport.list_backups.assert_called_once_with("dipp")
        mock_transport.disconnect.assert_called_once()
        assert "abc12345" in result.output
        assert "def67890" in result.output
