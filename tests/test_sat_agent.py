"""Tests for sat-agent."""

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


class TestStatusCommand:
    """Tests for the status command."""

    def test_status_returns_json_with_all_services(self, test_config):
        """Status command should return JSON with status of all services."""
        from sat_agent import get_status, load_config

        config = load_config(test_config)

        with patch('sat_agent.check_service_status') as mock_check:
            mock_check.return_value = 'running'
            result = get_status(config)

        assert result['status'] == 'ok'
        assert 'services' in result
        assert 'controller' in result['services']
        assert 'csp_server' in result['services']
        assert 'param_handler' in result['services']

    def test_status_reports_running_services(self, test_config):
        """Status should report 'running' for active services."""
        from sat_agent import get_status, load_config

        config = load_config(test_config)

        with patch('sat_agent.check_service_status') as mock_check:
            mock_check.return_value = 'running'
            result = get_status(config)

        assert result['services']['controller'] == 'running'

    def test_status_reports_stopped_services(self, test_config):
        """Status should report 'stopped' for inactive services."""
        from sat_agent import get_status, load_config

        config = load_config(test_config)

        with patch('sat_agent.check_service_status') as mock_check:
            mock_check.return_value = 'stopped'
            result = get_status(config)

        assert result['services']['controller'] == 'stopped'

    def test_status_handles_mixed_states(self, test_config):
        """Status should correctly report mixed running/stopped states."""
        from sat_agent import get_status, load_config

        config = load_config(test_config)

        def mock_status(service_name):
            if service_name == 'controller.service':
                return 'running'
            return 'stopped'

        with patch('sat_agent.check_service_status', side_effect=mock_status):
            result = get_status(config)

        assert result['services']['controller'] == 'running'
        assert result['services']['csp_server'] == 'stopped'
        assert result['services']['param_handler'] == 'stopped'


class TestCheckServiceStatus:
    """Tests for checking individual service status via systemctl."""

    def test_check_running_service(self):
        """Should return 'running' when systemctl reports active."""
        from sat_agent import check_service_status

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = check_service_status('test.service')

        assert result == 'running'
        mock_run.assert_called_once_with(
            ['systemctl', 'is-active', 'test.service'],
            capture_output=True,
            text=True
        )

    def test_check_stopped_service(self):
        """Should return 'stopped' when systemctl reports inactive."""
        from sat_agent import check_service_status

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=3)  # inactive
            result = check_service_status('test.service')

        assert result == 'stopped'


class TestLoadConfig:
    """Tests for loading configuration."""

    def test_load_config_returns_services(self, test_config):
        """Should load config and return services dict."""
        from sat_agent import load_config

        config = load_config(test_config)

        assert 'services' in config
        assert 'controller' in config['services']

    def test_load_config_missing_file(self, tmp_path):
        """Should raise error for missing config file."""
        from sat_agent import load_config

        missing = tmp_path / "missing.yaml"
        with pytest.raises(FileNotFoundError):
            load_config(missing)


class TestDependencyResolution:
    """Tests for dependency graph resolution."""

    def test_get_dependents_finds_direct_dependents(self, test_config):
        """Should find services that directly depend on given service."""
        from sat_agent import get_dependents, load_config

        config = load_config(test_config)
        # controller depends on csp_server
        dependents = get_dependents('csp_server', config)

        assert 'controller' in dependents

    def test_get_dependents_returns_empty_for_leaf(self, test_config):
        """Should return empty list for service with no dependents."""
        from sat_agent import get_dependents, load_config

        config = load_config(test_config)
        # Nothing depends on controller
        dependents = get_dependents('controller', config)

        assert dependents == []

    def test_get_dependents_finds_transitive_dependents(self, test_config):
        """Should find all transitive dependents."""
        from sat_agent import get_dependents, load_config

        config = load_config(test_config)
        # param_handler -> csp_server -> controller
        dependents = get_dependents('param_handler', config)

        assert 'csp_server' in dependents
        assert 'controller' in dependents

    def test_get_stop_order_includes_service_and_dependents(self, test_config):
        """Stop order should include service and all dependents, top-down."""
        from sat_agent import get_stop_order, load_config

        config = load_config(test_config)
        # Deploying csp_server: must stop controller first, then csp_server
        stop_order = get_stop_order('csp_server', config)

        assert 'controller' in stop_order
        assert 'csp_server' in stop_order
        # controller must come before csp_server (stop dependents first)
        assert stop_order.index('controller') < stop_order.index('csp_server')

    def test_get_stop_order_for_leaf_service(self, test_config):
        """Stop order for leaf service should only include itself."""
        from sat_agent import get_stop_order, load_config

        config = load_config(test_config)
        stop_order = get_stop_order('controller', config)

        assert stop_order == ['controller']

    def test_get_start_order_is_reverse_of_stop(self, test_config):
        """Start order should be reverse of stop order (bottom-up)."""
        from sat_agent import get_stop_order, get_start_order, load_config

        config = load_config(test_config)
        stop_order = get_stop_order('csp_server', config)
        start_order = get_start_order('csp_server', config)

        assert start_order == list(reversed(stop_order))


class TestServiceControl:
    """Tests for starting and stopping services."""

    def test_stop_service_calls_systemctl(self):
        """Should call systemctl stop with service name."""
        from sat_agent import stop_service

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            stop_service('controller.service')

        mock_run.assert_called_once_with(
            ['systemctl', 'stop', 'controller.service'],
            capture_output=True,
            text=True,
            check=True
        )

    def test_start_service_calls_systemctl(self):
        """Should call systemctl start with service name."""
        from sat_agent import start_service

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            start_service('controller.service')

        mock_run.assert_called_once_with(
            ['systemctl', 'start', 'controller.service'],
            capture_output=True,
            text=True,
            check=True
        )


class TestBinaryOperations:
    """Tests for binary backup and swap operations."""

    def test_backup_binary_copies_to_prev(self, test_config, tmp_path):
        """Should copy current binary to .prev in backup dir."""
        from sat_agent import backup_binary, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')

        # Create fake binary
        binary_dir = tmp_path / 'bin'
        binary_dir.mkdir()
        binary = binary_dir / 'controller'
        binary.write_text('binary content')

        # Override binary path in config
        config['services']['controller']['binary'] = str(binary)

        backup_binary('controller', config)

        backup_file = tmp_path / 'backups' / 'controller.prev'
        assert backup_file.exists()
        assert backup_file.read_text() == 'binary content'

    def test_backup_binary_skips_if_no_existing(self, test_config, tmp_path):
        """Should not error if binary doesn't exist (first deploy)."""
        from sat_agent import backup_binary, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')
        config['services']['controller']['binary'] = str(tmp_path / 'nonexistent')

        # Should not raise
        backup_binary('controller', config)

    def test_swap_binary_moves_new_to_final(self, tmp_path, test_config):
        """Should move .new file to final path."""
        from sat_agent import swap_binary, load_config

        config = load_config(test_config)

        # Create .new file
        binary = tmp_path / 'controller'
        new_binary = tmp_path / 'controller.new'
        new_binary.write_text('new binary')

        config['services']['controller']['binary'] = str(binary)

        swap_binary('controller', config)

        assert binary.exists()
        assert binary.read_text() == 'new binary'
        assert not new_binary.exists()

    def test_swap_binary_makes_executable(self, tmp_path, test_config):
        """Should chmod +x the deployed binary."""
        from sat_agent import swap_binary, load_config
        import stat

        config = load_config(test_config)

        binary = tmp_path / 'controller'
        new_binary = tmp_path / 'controller.new'
        new_binary.write_text('new binary')

        config['services']['controller']['binary'] = str(binary)

        swap_binary('controller', config)

        mode = binary.stat().st_mode
        assert mode & stat.S_IXUSR  # Owner execute

    def test_swap_binary_fails_if_new_missing(self, tmp_path, test_config):
        """Should raise error if .new file doesn't exist."""
        from sat_agent import swap_binary, load_config

        config = load_config(test_config)
        config['services']['controller']['binary'] = str(tmp_path / 'controller')

        with pytest.raises(FileNotFoundError):
            swap_binary('controller', config)


class TestDeployCommand:
    """Tests for the full deploy command."""

    def test_deploy_returns_success_json(self, test_config, tmp_path):
        """Deploy should return JSON with status ok."""
        from sat_agent import deploy, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')
        config['version_log'] = str(tmp_path / 'versions.json')

        # Setup binary files
        binary = tmp_path / 'controller'
        new_binary = tmp_path / 'controller.new'
        new_binary.write_text('new binary content')
        config['services']['controller']['binary'] = str(binary)

        with patch('sat_agent.stop_service'), \
             patch('sat_agent.start_service'), \
             patch('sat_agent.check_service_status', return_value='running'):
            result = deploy('controller', config)

        assert result['status'] == 'ok'
        assert result['service'] == 'controller'
        assert 'hash' in result

    def test_deploy_stops_services_in_order(self, test_config, tmp_path):
        """Deploy should stop dependents before service."""
        from sat_agent import deploy, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')
        config['version_log'] = str(tmp_path / 'versions.json')

        binary = tmp_path / 'csp_server'
        new_binary = tmp_path / 'csp_server.new'
        new_binary.write_text('new binary')
        config['services']['csp_server']['binary'] = str(binary)

        stop_calls = []

        def track_stop(svc):
            stop_calls.append(svc)

        with patch('sat_agent.stop_service', side_effect=track_stop), \
             patch('sat_agent.start_service'), \
             patch('sat_agent.check_service_status', return_value='running'):
            deploy('csp_server', config)

        # controller depends on csp_server, so stop controller first
        assert stop_calls.index('controller.service') < stop_calls.index('csp_server.service')

    def test_deploy_starts_services_in_reverse_order(self, test_config, tmp_path):
        """Deploy should start service before dependents."""
        from sat_agent import deploy, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')
        config['version_log'] = str(tmp_path / 'versions.json')

        binary = tmp_path / 'csp_server'
        new_binary = tmp_path / 'csp_server.new'
        new_binary.write_text('new binary')
        config['services']['csp_server']['binary'] = str(binary)

        start_calls = []

        def track_start(svc):
            start_calls.append(svc)

        with patch('sat_agent.stop_service'), \
             patch('sat_agent.start_service', side_effect=track_start), \
             patch('sat_agent.check_service_status', return_value='running'):
            deploy('csp_server', config)

        # Start csp_server first, then controller
        assert start_calls.index('csp_server.service') < start_calls.index('controller.service')

    def test_deploy_fails_if_service_not_running(self, test_config, tmp_path):
        """Deploy should fail if service doesn't start."""
        from sat_agent import deploy, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')
        config['version_log'] = str(tmp_path / 'versions.json')

        binary = tmp_path / 'controller'
        new_binary = tmp_path / 'controller.new'
        new_binary.write_text('new binary')
        config['services']['controller']['binary'] = str(binary)

        with patch('sat_agent.stop_service'), \
             patch('sat_agent.start_service'), \
             patch('sat_agent.check_service_status', return_value='stopped'):
            result = deploy('controller', config)

        assert result['status'] == 'failed'
        assert 'not running' in result['reason']

    def test_deploy_logs_to_versions_json(self, test_config, tmp_path):
        """Deploy should log deployment to versions.json."""
        from sat_agent import deploy, load_config
        import json

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')
        version_log = tmp_path / 'versions.json'
        config['version_log'] = str(version_log)

        binary = tmp_path / 'controller'
        new_binary = tmp_path / 'controller.new'
        new_binary.write_text('new binary content')
        config['services']['controller']['binary'] = str(binary)

        with patch('sat_agent.stop_service'), \
             patch('sat_agent.start_service'), \
             patch('sat_agent.check_service_status', return_value='running'):
            deploy('controller', config)

        assert version_log.exists()
        log_data = json.loads(version_log.read_text())
        assert len(log_data) == 1
        assert log_data[0]['service'] == 'controller'
        assert 'hash' in log_data[0]
        assert 'timestamp' in log_data[0]

    def test_deploy_unknown_service_fails(self, test_config):
        """Deploy should fail for unknown service."""
        from sat_agent import deploy, load_config

        config = load_config(test_config)

        result = deploy('unknown_service', config)

        assert result['status'] == 'failed'
        assert 'unknown' in result['reason'].lower()


class TestRollbackCommand:
    """Tests for the rollback command."""

    def test_rollback_returns_success_json(self, test_config, tmp_path):
        """Rollback should return JSON with status ok."""
        from sat_agent import rollback, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')

        # Setup binary and backup files
        binary = tmp_path / 'controller'
        binary.write_text('current binary')
        backup_dir = tmp_path / 'backups'
        backup_dir.mkdir()
        backup = backup_dir / 'controller.prev'
        backup.write_text('previous binary')

        config['services']['controller']['binary'] = str(binary)

        with patch('sat_agent.stop_service'), \
             patch('sat_agent.start_service'), \
             patch('sat_agent.check_service_status', return_value='running'):
            result = rollback('controller', config)

        assert result['status'] == 'ok'
        assert result['service'] == 'controller'
        assert 'hash' in result

    def test_rollback_copies_prev_to_binary(self, test_config, tmp_path):
        """Rollback should copy .prev file back to binary path."""
        from sat_agent import rollback, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')

        # Setup binary and backup files
        binary = tmp_path / 'controller'
        binary.write_text('current binary')
        backup_dir = tmp_path / 'backups'
        backup_dir.mkdir()
        backup = backup_dir / 'controller.prev'
        backup.write_text('previous binary')

        config['services']['controller']['binary'] = str(binary)

        with patch('sat_agent.stop_service'), \
             patch('sat_agent.start_service'), \
             patch('sat_agent.check_service_status', return_value='running'):
            rollback('controller', config)

        assert binary.read_text() == 'previous binary'

    def test_rollback_makes_executable(self, test_config, tmp_path):
        """Rollback should chmod +x the restored binary."""
        from sat_agent import rollback, load_config
        import stat

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')

        binary = tmp_path / 'controller'
        binary.write_text('current')
        backup_dir = tmp_path / 'backups'
        backup_dir.mkdir()
        backup = backup_dir / 'controller.prev'
        backup.write_text('previous')

        config['services']['controller']['binary'] = str(binary)

        with patch('sat_agent.stop_service'), \
             patch('sat_agent.start_service'), \
             patch('sat_agent.check_service_status', return_value='running'):
            rollback('controller', config)

        mode = binary.stat().st_mode
        assert mode & stat.S_IXUSR  # Owner execute

    def test_rollback_fails_if_no_backup(self, test_config, tmp_path):
        """Rollback should fail if no .prev file exists."""
        from sat_agent import rollback, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')

        binary = tmp_path / 'controller'
        binary.write_text('current')

        config['services']['controller']['binary'] = str(binary)

        with patch('sat_agent.stop_service'), \
             patch('sat_agent.start_service'):
            result = rollback('controller', config)

        assert result['status'] == 'failed'
        assert 'no backup' in result['reason'].lower()

    def test_rollback_unknown_service_fails(self, test_config):
        """Rollback should fail for unknown service."""
        from sat_agent import rollback, load_config

        config = load_config(test_config)

        result = rollback('unknown_service', config)

        assert result['status'] == 'failed'
        assert 'unknown' in result['reason'].lower()

    def test_rollback_stops_services_in_order(self, test_config, tmp_path):
        """Rollback should stop dependents before service."""
        from sat_agent import rollback, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')

        binary = tmp_path / 'csp_server'
        binary.write_text('current')
        backup_dir = tmp_path / 'backups'
        backup_dir.mkdir()
        backup = backup_dir / 'csp_server.prev'
        backup.write_text('previous')

        config['services']['csp_server']['binary'] = str(binary)

        stop_calls = []

        def track_stop(svc):
            stop_calls.append(svc)

        with patch('sat_agent.stop_service', side_effect=track_stop), \
             patch('sat_agent.start_service'), \
             patch('sat_agent.check_service_status', return_value='running'):
            rollback('csp_server', config)

        # controller depends on csp_server, so stop controller first
        assert stop_calls.index('controller.service') < stop_calls.index('csp_server.service')

    def test_rollback_starts_services_in_reverse_order(self, test_config, tmp_path):
        """Rollback should start service before dependents."""
        from sat_agent import rollback, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')

        binary = tmp_path / 'csp_server'
        binary.write_text('current')
        backup_dir = tmp_path / 'backups'
        backup_dir.mkdir()
        backup = backup_dir / 'csp_server.prev'
        backup.write_text('previous')

        config['services']['csp_server']['binary'] = str(binary)

        start_calls = []

        def track_start(svc):
            start_calls.append(svc)

        with patch('sat_agent.stop_service'), \
             patch('sat_agent.start_service', side_effect=track_start), \
             patch('sat_agent.check_service_status', return_value='running'):
            rollback('csp_server', config)

        # Start csp_server first, then controller
        assert start_calls.index('csp_server.service') < start_calls.index('controller.service')

    def test_rollback_fails_if_service_not_running_after(self, test_config, tmp_path):
        """Rollback should fail if service doesn't start."""
        from sat_agent import rollback, load_config

        config = load_config(test_config)
        config['backup_dir'] = str(tmp_path / 'backups')

        binary = tmp_path / 'controller'
        binary.write_text('current')
        backup_dir = tmp_path / 'backups'
        backup_dir.mkdir()
        backup = backup_dir / 'controller.prev'
        backup.write_text('previous')

        config['services']['controller']['binary'] = str(binary)

        with patch('sat_agent.stop_service'), \
             patch('sat_agent.start_service'), \
             patch('sat_agent.check_service_status', return_value='stopped'):
            result = rollback('controller', config)

        assert result['status'] == 'failed'
        assert 'not running' in result['reason']


class TestMainCLI:
    """Tests for the main CLI entry point."""

    def test_main_handles_rollback_command(self, test_config, tmp_path, monkeypatch, capsys):
        """Main should handle rollback command and print JSON."""
        from sat_agent import main
        import sys

        # Setup
        backup_dir = tmp_path / 'backups'
        backup_dir.mkdir()
        binary = tmp_path / 'controller'
        binary.write_text('current')
        backup = backup_dir / 'controller.prev'
        backup.write_text('previous')

        # Create config with correct paths
        config_content = f"""
services:
  controller:
    binary: {binary}
    systemd: controller.service
    depends_on: []

backup_dir: {backup_dir}
"""
        config_file = tmp_path / 'config.yaml'
        config_file.write_text(config_content)

        monkeypatch.setenv('SAT_AGENT_CONFIG', str(config_file))
        monkeypatch.setattr(sys, 'argv', ['sat_agent', 'rollback', 'controller'])

        with patch('sat_agent.stop_service'), \
             patch('sat_agent.start_service'), \
             patch('sat_agent.check_service_status', return_value='running'):
            main()

        captured = capsys.readouterr()
        response = json.loads(captured.out)
        assert response['status'] == 'ok'
        assert response['service'] == 'controller'
