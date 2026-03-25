"""Tests for the satdeploy init command."""

import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from satdeploy.cli import main
from satdeploy.output import SYMBOLS


class TestInitCommand:
    """Test the init command."""

    def test_init_command_exists(self):
        """The init command should exist."""
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--help"])
        assert result.exit_code == 0
        assert "Interactive setup" in result.output or "config" in result.output.lower()

    def test_init_creates_config_file(self, tmp_path):
        """Init should create a config.yaml file."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="\n\n192.168.1.50\nroot\n",  # name(default), transport(ssh), host, user
        )

        assert result.exit_code == 0
        assert (config_dir / "config.yaml").exists()

    def test_init_prompts_for_host(self, tmp_path):
        """Init should prompt for target host."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="\n\n192.168.1.50\nroot\n",  # name(default), transport(ssh), host, user
        )

        assert "host" in result.output.lower() or "Target host" in result.output

    def test_init_prompts_for_user(self, tmp_path):
        """Init should prompt for target user."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="\n\n192.168.1.50\nroot\n",  # name(default), transport(ssh), host, user
        )

        assert "user" in result.output.lower()

    def test_init_saves_user_input(self, tmp_path):
        """Init should save the user input to config file."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="som1\n\n10.0.0.100\nadmin\n",  # name, transport(ssh), host, user
        )

        config_file = config_dir / "config.yaml"
        config = yaml.safe_load(config_file.read_text())

        assert config["name"] == "som1"
        assert config["host"] == "10.0.0.100"
        assert config["user"] == "admin"
        assert config["transport"] == "ssh"

    def test_init_sets_defaults(self, tmp_path):
        """Init should set default values for backup_dir and max_backups."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="\n\n192.168.1.50\nroot\n",  # name(default), transport(ssh), host, user
        )

        config_file = config_dir / "config.yaml"
        config = yaml.safe_load(config_file.read_text())

        assert config["backup_dir"] == "/opt/satdeploy/backups"
        assert config["max_backups"] == 10
        assert "my_app" in config["apps"]

    def test_init_csp_transport(self, tmp_path):
        """Init should support CSP transport configuration."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            # name, csp, zmq_endpoint, agent_node, ground_node, appsys_node
            input="sat1\ncsp\ntcp://localhost:4040\n5424\n4040\n10\n",
        )

        config_file = config_dir / "config.yaml"
        config = yaml.safe_load(config_file.read_text())

        assert config["name"] == "sat1"
        assert config["transport"] == "csp"
        assert config["zmq_endpoint"] == "tcp://localhost:4040"
        assert config["agent_node"] == 5424
        assert config["ground_node"] == 4040
        assert config["appsys_node"] == 10

    def test_init_warns_if_config_exists(self, tmp_path):
        """Init should warn if config already exists."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("host: old\n")

        result = runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="n\n",  # Don't overwrite
        )

        # Should ask about overwriting
        assert "exist" in result.output.lower() or "overwrite" in result.output.lower()

    def test_init_can_overwrite_existing_config(self, tmp_path):
        """Init should overwrite config if user confirms."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("name: old\nhost: old\nuser: old\n")

        result = runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="y\n\n\n192.168.1.50\nroot\n",  # Overwrite, name(default), transport(ssh), host, user
        )

        config = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert config["host"] == "192.168.1.50"


class TestInitPolishedOutput:
    """Tests for polished CLI output formatting."""

    def test_init_success_shows_checkmark(self, tmp_path):
        """Init should show checkmark when config is saved."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="\n\n192.168.1.50\nroot\n",  # name(default), transport(ssh), host, user
            color=True,
        )

        assert result.exit_code == 0
        assert SYMBOLS["check"] in result.output
