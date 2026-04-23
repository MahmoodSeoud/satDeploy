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
            input="\n192.168.1.50\nroot\n",  # name(default), host, user
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
            input="\n192.168.1.50\nroot\n",
        )

        assert "host" in result.output.lower() or "Target host" in result.output

    def test_init_prompts_for_user(self, tmp_path):
        """Init should prompt for target user."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="\n192.168.1.50\nroot\n",
        )

        assert "user" in result.output.lower()

    def test_init_saves_user_input(self, tmp_path):
        """Init should save the user input to config file."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="som1\n10.0.0.100\nadmin\n",  # name, host, user
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
            input="\n192.168.1.50\nroot\n",
        )

        config_file = config_dir / "config.yaml"
        config = yaml.safe_load(config_file.read_text())

        assert config["backup_dir"] == "/opt/satdeploy/backups"
        assert config["max_backups"] == 10
        assert "example_app" in config["apps"]

    def test_init_does_not_prompt_for_csp(self, tmp_path):
        """Post-cd38042 the Python CLI doesn't support transport=csp.
        Init must not offer CSP — it would produce a config that fails
        on push/iterate at cli.py:75. CSP teams use satdeploy-apm
        inside CSH instead."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="\n192.168.1.50\nroot\n",
        )

        # No prompt should ask for transport or ZMQ endpoint.
        assert "Transport type" not in result.output
        assert "ZMQ endpoint" not in result.output
        # The intro mentions CSP only to point users at the APM.
        assert "satdeploy-apm" in result.output

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

        runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="y\n\n192.168.1.50\nroot\n",  # Overwrite, name(default), host, user
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
            input="\n192.168.1.50\nroot\n",
            color=True,
        )

        assert result.exit_code == 0
        assert SYMBOLS["check"] in result.output

    def test_init_prints_next_steps(self, tmp_path):
        """Init should end with a next-steps block pointing the user
        at iterate + the SATDEPLOY_SDK env var for the ABI check."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["init", "--config", str(config_dir / "config.yaml")],
            input="\n192.168.1.50\nroot\n",
        )

        assert result.exit_code == 0
        assert "Next steps" in result.output
        assert "satdeploy iterate" in result.output
        assert "SATDEPLOY_SDK" in result.output
