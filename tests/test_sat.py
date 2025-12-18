"""Tests for sat CLI."""

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def test_config(tmp_path):
    """Create a test config file."""
    config_content = """
flatsat:
  host: flatsat-disco.local
  user: root

backup_dir: /opt/sat-agent/backups
version_log: /opt/sat-agent/versions.json

services:
  controller:
    binary: /opt/disco/bin/controller
    systemd: controller.service
    depends_on:
      - csp_server

  csp_server:
    binary: /opt/disco/bin/csp_server
    systemd: csp_server.service
    depends_on:
      - param_handler

  param_handler:
    binary: /opt/disco/bin/param_handler
    systemd: param_handler.service
    depends_on: []
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(config_content)
    return config_file


class TestLoadConfig:
    """Tests for loading configuration."""

    def test_load_config_returns_flatsat_settings(self, test_config):
        """Should load config and return flatsat settings."""
        from sat import load_config

        config = load_config(test_config)

        assert 'flatsat' in config
        assert config['flatsat']['host'] == 'flatsat-disco.local'
        assert config['flatsat']['user'] == 'root'

    def test_load_config_returns_services(self, test_config):
        """Should load config and return services dict."""
        from sat import load_config

        config = load_config(test_config)

        assert 'services' in config
        assert 'controller' in config['services']

    def test_load_config_missing_file(self, tmp_path):
        """Should raise error for missing config file."""
        from sat import load_config

        missing = tmp_path / "missing.yaml"
        with pytest.raises(FileNotFoundError):
            load_config(missing)


class TestSshRun:
    """Tests for SSH command execution."""

    def test_ssh_run_constructs_correct_command(self, test_config):
        """Should construct correct SSH command."""
        from sat import ssh_run, load_config

        config = load_config(test_config)

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout='output',
                stderr='',
                returncode=0
            )
            ssh_run(config, 'test command')

        mock_run.assert_called_once_with(
            ['ssh', 'root@flatsat-disco.local', 'test command'],
            capture_output=True,
            text=True
        )

    def test_ssh_run_returns_output(self, test_config):
        """Should return stdout, stderr, and returncode."""
        from sat import ssh_run, load_config

        config = load_config(test_config)

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                stdout='output data',
                stderr='error data',
                returncode=1
            )
            stdout, stderr, returncode = ssh_run(config, 'test')

        assert stdout == 'output data'
        assert stderr == 'error data'
        assert returncode == 1


class TestRsyncUpload:
    """Tests for rsync upload functionality."""

    def test_rsync_constructs_correct_command(self, test_config, tmp_path):
        """Should construct correct rsync command."""
        from sat import rsync_upload, load_config

        config = load_config(test_config)

        local_file = tmp_path / 'test_binary'
        local_file.write_text('content')

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            rsync_upload(config, str(local_file), 'controller')

        expected_cmd = [
            'rsync', '-az', '--progress',
            str(local_file),
            'root@flatsat-disco.local:/opt/disco/bin/controller.new'
        ]
        mock_run.assert_called_once_with(
            expected_cmd,
            capture_output=True,
            text=True
        )

    def test_rsync_returns_success(self, test_config, tmp_path):
        """Should return success tuple on success."""
        from sat import rsync_upload, load_config

        config = load_config(test_config)

        local_file = tmp_path / 'test_binary'
        local_file.write_text('content')

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            success, error = rsync_upload(config, str(local_file), 'controller')

        assert success is True
        assert error is None

    def test_rsync_returns_failure_on_error(self, test_config, tmp_path):
        """Should return failure tuple on rsync error."""
        from sat import rsync_upload, load_config

        config = load_config(test_config)

        local_file = tmp_path / 'test_binary'
        local_file.write_text('content')

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stderr='connection refused'
            )
            success, error = rsync_upload(config, str(local_file), 'controller')

        assert success is False
        assert 'connection refused' in error

    def test_rsync_fails_for_unknown_service(self, test_config, tmp_path):
        """Should fail for unknown service."""
        from sat import rsync_upload, load_config

        config = load_config(test_config)

        local_file = tmp_path / 'test_binary'
        local_file.write_text('content')

        success, error = rsync_upload(config, str(local_file), 'unknown_service')

        assert success is False
        assert 'unknown_service' in error


class TestStatusCommand:
    """Tests for the status command."""

    def test_status_calls_agent_via_ssh(self, test_config):
        """Status should call sat-agent status via SSH."""
        from sat import cmd_status, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'ok', 'services': {'controller': 'running'}}),
                '',
                0
            )
            cmd_status(config)

        mock_ssh.assert_called_once_with(
            config,
            '/opt/sat-agent/sat-agent status'
        )

    def test_status_returns_0_on_success(self, test_config):
        """Status should return 0 on success."""
        from sat import cmd_status, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'ok', 'services': {'controller': 'running'}}),
                '',
                0
            )
            result = cmd_status(config)

        assert result == 0

    def test_status_returns_1_on_ssh_failure(self, test_config):
        """Status should return 1 when SSH fails."""
        from sat import cmd_status, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = ('', 'connection refused', 1)
            result = cmd_status(config)

        assert result == 1

    def test_status_returns_1_on_invalid_json(self, test_config):
        """Status should return 1 when agent returns invalid JSON."""
        from sat import cmd_status, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = ('not json', '', 0)
            result = cmd_status(config)

        assert result == 1

    def test_status_returns_1_on_agent_error(self, test_config):
        """Status should return 1 when agent reports error."""
        from sat import cmd_status, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'failed', 'reason': 'config error'}),
                '',
                0
            )
            result = cmd_status(config)

        assert result == 1


class TestDeployCommand:
    """Tests for the deploy command."""

    def test_deploy_uploads_then_calls_agent(self, test_config, tmp_path):
        """Deploy should upload binary then call agent deploy."""
        from sat import cmd_deploy, load_config

        config = load_config(test_config)

        binary = tmp_path / 'controller'
        binary.write_text('binary')

        call_order = []

        def track_rsync(*args, **kwargs):
            call_order.append('rsync')
            return (True, None)

        def track_ssh(*args, **kwargs):
            call_order.append('ssh')
            return (
                json.dumps({'status': 'ok', 'service': 'controller', 'hash': 'abc123'}),
                '',
                0
            )

        with patch('sat.rsync_upload', side_effect=track_rsync), \
             patch('sat.ssh_run', side_effect=track_ssh):
            cmd_deploy(config, 'controller', str(binary))

        assert call_order == ['rsync', 'ssh']

    def test_deploy_returns_0_on_success(self, test_config, tmp_path):
        """Deploy should return 0 on success."""
        from sat import cmd_deploy, load_config

        config = load_config(test_config)

        binary = tmp_path / 'controller'
        binary.write_text('binary')

        with patch('sat.rsync_upload', return_value=(True, None)), \
             patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'ok', 'service': 'controller', 'hash': 'abc123'}),
                '',
                0
            )
            result = cmd_deploy(config, 'controller', str(binary))

        assert result == 0

    def test_deploy_returns_1_for_unknown_service(self, test_config, tmp_path):
        """Deploy should return 1 for unknown service."""
        from sat import cmd_deploy, load_config

        config = load_config(test_config)

        binary = tmp_path / 'controller'
        binary.write_text('binary')

        result = cmd_deploy(config, 'unknown_service', str(binary))

        assert result == 1

    def test_deploy_returns_1_for_missing_binary(self, test_config, tmp_path):
        """Deploy should return 1 when binary doesn't exist."""
        from sat import cmd_deploy, load_config

        config = load_config(test_config)

        result = cmd_deploy(config, 'controller', str(tmp_path / 'nonexistent'))

        assert result == 1

    def test_deploy_returns_1_on_upload_failure(self, test_config, tmp_path):
        """Deploy should return 1 when upload fails."""
        from sat import cmd_deploy, load_config

        config = load_config(test_config)

        binary = tmp_path / 'controller'
        binary.write_text('binary')

        with patch('sat.rsync_upload', return_value=(False, 'connection refused')):
            result = cmd_deploy(config, 'controller', str(binary))

        assert result == 1

    def test_deploy_returns_1_on_agent_failure(self, test_config, tmp_path):
        """Deploy should return 1 when agent reports failure."""
        from sat import cmd_deploy, load_config

        config = load_config(test_config)

        binary = tmp_path / 'controller'
        binary.write_text('binary')

        with patch('sat.rsync_upload', return_value=(True, None)), \
             patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'failed', 'reason': 'service crashed'}),
                '',
                0
            )
            result = cmd_deploy(config, 'controller', str(binary))

        assert result == 1


class TestRollbackCommand:
    """Tests for the rollback command."""

    def test_rollback_calls_agent_via_ssh(self, test_config):
        """Rollback should call sat-agent rollback via SSH."""
        from sat import cmd_rollback, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'ok', 'service': 'controller', 'hash': 'abc123'}),
                '',
                0
            )
            cmd_rollback(config, 'controller')

        mock_ssh.assert_called_once_with(
            config,
            '/opt/sat-agent/sat-agent rollback controller'
        )

    def test_rollback_returns_0_on_success(self, test_config):
        """Rollback should return 0 on success."""
        from sat import cmd_rollback, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'ok', 'service': 'controller', 'hash': 'abc123'}),
                '',
                0
            )
            result = cmd_rollback(config, 'controller')

        assert result == 0

    def test_rollback_returns_1_on_ssh_failure(self, test_config):
        """Rollback should return 1 when SSH fails."""
        from sat import cmd_rollback, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = ('', 'connection refused', 1)
            result = cmd_rollback(config, 'controller')

        assert result == 1

    def test_rollback_returns_1_on_agent_error(self, test_config):
        """Rollback should return 1 when agent reports error."""
        from sat import cmd_rollback, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'failed', 'reason': 'No backup found'}),
                '',
                0
            )
            result = cmd_rollback(config, 'controller')

        assert result == 1

    def test_rollback_returns_1_for_unknown_service(self, test_config):
        """Rollback should return 1 for unknown service."""
        from sat import cmd_rollback, load_config

        config = load_config(test_config)

        result = cmd_rollback(config, 'unknown_service')

        assert result == 1


class TestTimingOutput:
    """Tests for timing output functionality."""

    def test_deploy_prints_timing(self, test_config, tmp_path, capsys):
        """Deploy should print timing in output."""
        from sat import cmd_deploy, load_config

        config = load_config(test_config)

        binary = tmp_path / 'controller'
        binary.write_text('binary')

        with patch('sat.rsync_upload', return_value=(True, None)), \
             patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'ok', 'service': 'controller', 'hash': 'abc123'}),
                '',
                0
            )
            cmd_deploy(config, 'controller', str(binary))

        captured = capsys.readouterr()
        # Should contain timing info like "in 0.5s" or "in 1s"
        assert ' in ' in captured.out
        assert 's)' in captured.out

    def test_format_duration_seconds(self):
        """Should format seconds correctly."""
        from sat import format_duration

        assert format_duration(0.5) == '0.5s'
        assert format_duration(30.0) == '30.0s'
        assert format_duration(59.9) == '59.9s'

    def test_format_duration_minutes(self):
        """Should format minutes and seconds correctly."""
        from sat import format_duration

        assert format_duration(60) == '1m 0s'
        assert format_duration(90) == '1m 30s'
        assert format_duration(125) == '2m 5s'


class TestLogsCommand:
    """Tests for the logs command."""

    def test_logs_calls_ssh_with_journalctl(self, test_config):
        """Logs should call SSH with journalctl -f command."""
        from sat import cmd_logs, load_config

        config = load_config(test_config)

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            cmd_logs(config, 'controller')

        # Should call ssh with journalctl -f -u <service>
        call_args = mock_run.call_args
        assert 'ssh' in call_args[0][0]
        assert 'journalctl' in call_args[0][0][2]
        assert '-f' in call_args[0][0][2]
        assert 'controller.service' in call_args[0][0][2]

    def test_logs_returns_0_on_success(self, test_config):
        """Logs should return 0 on success (user exits)."""
        from sat import cmd_logs, load_config

        config = load_config(test_config)

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = cmd_logs(config, 'controller')

        assert result == 0

    def test_logs_returns_1_for_unknown_service(self, test_config):
        """Logs should return 1 for unknown service."""
        from sat import cmd_logs, load_config

        config = load_config(test_config)

        result = cmd_logs(config, 'unknown_service')

        assert result == 1


class TestRestartCommand:
    """Tests for the restart command."""

    def test_restart_calls_agent_via_ssh(self, test_config):
        """Restart should call sat-agent restart via SSH."""
        from sat import cmd_restart, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'ok', 'service': 'controller'}),
                '',
                0
            )
            cmd_restart(config, 'controller')

        mock_ssh.assert_called_once_with(
            config,
            '/opt/sat-agent/sat-agent restart controller'
        )

    def test_restart_returns_0_on_success(self, test_config):
        """Restart should return 0 on success."""
        from sat import cmd_restart, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'ok', 'service': 'controller'}),
                '',
                0
            )
            result = cmd_restart(config, 'controller')

        assert result == 0

    def test_restart_returns_1_on_ssh_failure(self, test_config):
        """Restart should return 1 when SSH fails."""
        from sat import cmd_restart, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = ('', 'connection refused', 1)
            result = cmd_restart(config, 'controller')

        assert result == 1

    def test_restart_returns_1_on_agent_error(self, test_config):
        """Restart should return 1 when agent reports error."""
        from sat import cmd_restart, load_config

        config = load_config(test_config)

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'failed', 'reason': 'Service crashed'}),
                '',
                0
            )
            result = cmd_restart(config, 'controller')

        assert result == 1

    def test_restart_returns_1_for_unknown_service(self, test_config):
        """Restart should return 1 for unknown service."""
        from sat import cmd_restart, load_config

        config = load_config(test_config)

        result = cmd_restart(config, 'unknown_service')

        assert result == 1


class TestMainCLI:
    """Tests for the main CLI entry point."""

    def test_main_handles_rollback_command(self, test_config, monkeypatch):
        """Main should handle rollback command."""
        from sat import main
        import sys

        monkeypatch.setenv('SAT_CONFIG', str(test_config))
        monkeypatch.setattr(sys, 'argv', ['sat', 'rollback', 'controller'])

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'ok', 'service': 'controller', 'hash': 'abc123'}),
                '',
                0
            )
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 0

    def test_main_handles_logs_command(self, test_config, monkeypatch):
        """Main should handle logs command."""
        from sat import main
        import sys

        monkeypatch.setenv('SAT_CONFIG', str(test_config))
        monkeypatch.setattr(sys, 'argv', ['sat', 'logs', 'controller'])

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 0

    def test_main_handles_restart_command(self, test_config, monkeypatch):
        """Main should handle restart command."""
        from sat import main
        import sys

        monkeypatch.setenv('SAT_CONFIG', str(test_config))
        monkeypatch.setattr(sys, 'argv', ['sat', 'restart', 'controller'])

        with patch('sat.ssh_run') as mock_ssh:
            mock_ssh.return_value = (
                json.dumps({'status': 'ok', 'service': 'controller'}),
                '',
                0
            )
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 0
