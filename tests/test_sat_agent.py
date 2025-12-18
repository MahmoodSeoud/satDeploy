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
