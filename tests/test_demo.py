"""Tests for the demo mode feature."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.demo import (
    DEMO_DIR,
    DEMO_AGENT_NODE,
    DEMO_GROUND_NODE,
    DEMO_ZMQ_PUB_PORT,
    DEMO_ZMQ_SUB_PORT,
    DEMO_CONFIG,
    GHCR_IMAGE,
    _check_docker,
    _find_demo_binary,
    _is_agent_container_running,
    _find_repo_compose,
    _write_demo_config,
    _copy_demo_binary,
    _wait_for_agent,
    demo_start,
    demo_stop,
    demo_status,
)
from satdeploy.output import SatDeployError
from satdeploy.transport.base import TransportError


class TestCheckDocker:
    def test_docker_not_installed(self):
        with patch("satdeploy.demo.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(SatDeployError, match="Docker not found"):
                _check_docker()

    def test_docker_compose_not_available(self):
        mock_result = MagicMock(returncode=1)
        with patch("satdeploy.demo.subprocess.run", return_value=mock_result):
            with pytest.raises(SatDeployError, match="Docker Compose v2 not found"):
                _check_docker()

    def test_docker_not_running(self):
        compose_ok = MagicMock(returncode=0)
        daemon_fail = MagicMock(returncode=1)

        def side_effect(cmd, **kwargs):
            if "compose" in cmd:
                return compose_ok
            return daemon_fail

        with patch("satdeploy.demo.subprocess.run", side_effect=side_effect):
            with pytest.raises(SatDeployError, match="Docker daemon is not running"):
                _check_docker()

    def test_docker_ok(self):
        mock_result = MagicMock(returncode=0)
        with patch("satdeploy.demo.subprocess.run", return_value=mock_result):
            _check_docker()  # Should not raise


class TestFindDemoBinary:
    def test_finds_binary_in_repo(self):
        path = _find_demo_binary("v2")
        assert path.exists()
        assert path.name == "test_app"

    def test_missing_version_raises(self):
        with pytest.raises(SatDeployError, match="Demo binary not found"):
            _find_demo_binary("v99")


class TestFindRepoCompose:
    def test_always_returns_none_for_standalone_demo(self):
        # Demo always uses standalone mode (pre-built sim image),
        # so _find_repo_compose always returns None
        compose = _find_repo_compose()
        assert compose is None


class TestIsAgentContainerRunning:
    def test_agent_running(self, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}")
        mock_result = MagicMock(returncode=0, stdout="satbuild-agent-1\n")
        with patch("satdeploy.demo.subprocess.run", return_value=mock_result):
            assert _is_agent_container_running(compose_file) is True

    def test_agent_not_running(self, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}")
        mock_result = MagicMock(returncode=0, stdout="")
        with patch("satdeploy.demo.subprocess.run", return_value=mock_result):
            assert _is_agent_container_running(compose_file) is False

    def test_compose_error(self, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}")
        mock_result = MagicMock(returncode=1, stdout="")
        with patch("satdeploy.demo.subprocess.run", return_value=mock_result):
            assert _is_agent_container_running(compose_file) is False


class TestWriteDemoConfig:
    def test_creates_config(self, tmp_path):
        demo_config_path = tmp_path / ".demo-config.yaml"
        with patch("satdeploy.demo.DEMO_DIR", tmp_path), \
             patch("satdeploy.demo.DEMO_CONFIG_PATH", demo_config_path):
            _write_demo_config()
            assert demo_config_path.exists()
            data = yaml.safe_load(demo_config_path.read_text())
            assert data["name"] == "demo-satellite"
            assert data["transport"] == "csp"
            assert data["agent_node"] == DEMO_AGENT_NODE
            assert "test_app" in data["apps"]


class TestCopyDemoBinary:
    def test_copies_v2_binary(self, tmp_path):
        with patch("satdeploy.demo.DEMO_DIR", tmp_path):
            _copy_demo_binary()
            dest = tmp_path / "binaries" / "test_app"
            assert dest.exists()
            content = dest.read_text()
            assert "v2.0.0" in content


class TestWaitForAgent:
    def test_agent_responds_immediately(self):
        mock_transport = MagicMock()
        mock_transport.get_status.return_value = {"test_app": MagicMock()}

        with patch("satdeploy.demo.CSPTransport", return_value=mock_transport):
            result = _wait_for_agent(max_attempts=3, interval=0.01)
            assert result is True
            mock_transport.connect.assert_called_once()
            mock_transport.disconnect.assert_called_once()

    def test_agent_responds_after_retries(self):
        mock_transport = MagicMock()
        mock_transport.get_status.side_effect = [
            {},  # empty = not ready (but is a dict, so returns True)
            {"test_app": MagicMock()},
        ]

        with patch("satdeploy.demo.CSPTransport", return_value=mock_transport):
            result = _wait_for_agent(max_attempts=5, interval=0.01)
            assert result is True

    def test_agent_timeout(self):
        mock_transport = MagicMock()
        mock_transport.get_status.side_effect = TransportError("timeout")

        with patch("satdeploy.demo.CSPTransport", return_value=mock_transport):
            result = _wait_for_agent(max_attempts=3, interval=0.01)
            assert result is False
            mock_transport.disconnect.assert_called_once()

    def test_connect_failure(self):
        mock_transport = MagicMock()
        mock_transport.connect.side_effect = TransportError("fail")

        with patch("satdeploy.demo.CSPTransport", return_value=mock_transport):
            result = _wait_for_agent(max_attempts=3, interval=0.01)
            assert result is False


class TestDemoStart:
    def test_start_already_running(self, tmp_path, capsys):
        """When demo is already running, just re-print the tutorial."""
        with patch("satdeploy.demo._check_docker"):
            with patch("satdeploy.demo._get_compose_file", return_value=tmp_path / "dc.yml"):
                with patch("satdeploy.demo._is_agent_container_running", return_value=True):
                    # Create fake config to indicate demo is set up
                    with patch("satdeploy.demo.DEMO_DIR", tmp_path):
                        (tmp_path / "config.yaml").write_text("name: demo")
                        (tmp_path / "dc.yml").write_text("services: {}")
                        demo_start()
                        output = capsys.readouterr().out
                        assert "already running" in output.lower()

    def test_start_no_docker(self):
        with patch("satdeploy.demo._check_docker",
                    side_effect=SatDeployError("Docker not found")):
            with pytest.raises(SatDeployError, match="Docker not found"):
                demo_start()


class TestDemoStop:
    def test_stop_repo_mode(self, capsys):
        """In repo mode, demo stop doesn't stop containers."""
        repo_compose = Path("/fake/docker-compose.yml")
        with patch("satdeploy.demo._get_compose_file", return_value=repo_compose):
            with patch("satdeploy.demo._find_repo_compose", return_value=repo_compose):
                with patch("satdeploy.demo.DEMO_DIR", Path("/tmp/nonexistent")):
                    demo_stop()
                    output = capsys.readouterr().out
                    assert "leaving containers running" in output.lower()

    def test_stop_clean(self, tmp_path):
        with patch("satdeploy.demo._get_compose_file", return_value=tmp_path / "dc.yml"):
            with patch("satdeploy.demo._find_repo_compose", return_value=None):
                with patch("satdeploy.demo.DEMO_DIR", tmp_path):
                    tmp_path.mkdir(exist_ok=True)
                    demo_stop(clean=True)
                    assert not tmp_path.exists()


class TestDemoStatus:
    def test_status_running(self, tmp_path, capsys):
        compose_file = tmp_path / "docker-compose.yml"
        compose_file.write_text("services: {}")

        with patch("satdeploy.demo._get_compose_file", return_value=compose_file):
            with patch("satdeploy.demo._is_agent_container_running", return_value=True):
                with patch("satdeploy.demo._find_repo_compose", return_value=compose_file):
                    demo_status()
                    output = capsys.readouterr().out
                    assert "running" in output.lower()
                    assert str(DEMO_AGENT_NODE) in output

    def test_status_not_running(self, tmp_path, capsys):
        with patch("satdeploy.demo._get_compose_file", return_value=tmp_path / "dc.yml"):
            with patch("satdeploy.demo._is_agent_container_running", return_value=False):
                demo_status()
                output = capsys.readouterr().out
                assert "not running" in output.lower()


class TestConfigDirEnvvar:
    def test_envvar_sets_config_dir(self, tmp_path):
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        config_file = config_dir / "config.yaml"
        config_file.write_text(yaml.dump({
            "name": "test",
            "transport": "ssh",
            "host": "test-host",
            "user": "test-user",
            "apps": {},
        }))

        runner = CliRunner()
        result = runner.invoke(
            main, ["config"],
            env={"SATDEPLOY_CONFIG": str(config_file)},
        )
        assert result.exit_code == 0
        assert "test-host" in result.output

    def test_flag_overrides_envvar(self, tmp_path):
        flag_dir = tmp_path / "flag"
        flag_dir.mkdir()
        (flag_dir / "config.yaml").write_text(yaml.dump({
            "name": "flag-target",
            "transport": "ssh",
            "host": "flag-host",
            "user": "test",
            "apps": {},
        }))

        env_dir = tmp_path / "env"
        env_dir.mkdir()
        (env_dir / "config.yaml").write_text(yaml.dump({
            "name": "env-target",
            "transport": "ssh",
            "host": "env-host",
            "user": "test",
            "apps": {},
        }))

        runner = CliRunner()
        result = runner.invoke(
            main, ["config", "--config", str(flag_dir / "config.yaml")],
            env={"SATDEPLOY_CONFIG": str(env_dir / "config.yaml")},
        )
        assert result.exit_code == 0
        assert "flag-host" in result.output


class TestDemoCLI:
    def test_demo_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["demo", "--help"])
        assert result.exit_code == 0
        assert "start" in result.output
        assert "stop" in result.output
        assert "status" in result.output
        assert "shell" in result.output
        assert "eject" in result.output

    def test_demo_start_invokes_module(self):
        runner = CliRunner()
        with patch("satdeploy.demo.demo_start") as mock_start:
            result = runner.invoke(main, ["demo", "start"])
            mock_start.assert_called_once()

    def test_demo_stop_invokes_module(self):
        runner = CliRunner()
        with patch("satdeploy.demo.demo_stop") as mock_stop:
            result = runner.invoke(main, ["demo", "stop"])
            mock_stop.assert_called_once_with(clean=False)

    def test_demo_stop_clean_invokes_module(self):
        runner = CliRunner()
        with patch("satdeploy.demo.demo_stop") as mock_stop:
            result = runner.invoke(main, ["demo", "stop", "--clean"])
            mock_stop.assert_called_once_with(clean=True)

    def test_demo_shell_invokes_module(self):
        runner = CliRunner()
        with patch("satdeploy.demo.demo_shell") as mock_shell:
            result = runner.invoke(main, ["demo", "shell"])
            mock_shell.assert_called_once()

    def test_demo_status_invokes_module(self):
        runner = CliRunner()
        with patch("satdeploy.demo.demo_status") as mock_status:
            result = runner.invoke(main, ["demo", "status"])
            mock_status.assert_called_once()
