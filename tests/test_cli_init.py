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
            ["init", "--config-dir", str(config_dir)],
            input="\n192.168.1.50\nroot\n\n",  # module, host, user, no more modules
        )

        assert result.exit_code == 0
        assert (config_dir / "config.yaml").exists()

    def test_init_prompts_for_host(self, tmp_path):
        """Init should prompt for target host."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["init", "--config-dir", str(config_dir)],
            input="\n192.168.1.50\nroot\n\n",  # module, host, user, no more modules
        )

        assert "host" in result.output.lower() or "Target host" in result.output

    def test_init_prompts_for_user(self, tmp_path):
        """Init should prompt for target user."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["init", "--config-dir", str(config_dir)],
            input="\n192.168.1.50\nroot\n\n",  # module, host, user, no more modules
        )

        assert "user" in result.output.lower()

    def test_init_saves_user_input(self, tmp_path):
        """Init should save the user input to config file."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        runner.invoke(
            main,
            ["init", "--config-dir", str(config_dir)],
            input="som1\n10.0.0.100\nadmin\n\n",  # module, host, user, no more modules
        )

        config_file = config_dir / "config.yaml"
        config = yaml.safe_load(config_file.read_text())

        assert config["modules"]["som1"]["host"] == "10.0.0.100"
        assert config["modules"]["som1"]["user"] == "admin"

    def test_init_saves_multiple_modules(self, tmp_path):
        """Init should save multiple modules when user adds more."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        runner.invoke(
            main,
            ["init", "--config-dir", str(config_dir)],
            # som1, host, user, yes add more, som2, host, user, no more
            input="som1\n10.0.0.100\nroot\ny\nsom2\n10.0.0.101\nroot\n\n",
        )

        config_file = config_dir / "config.yaml"
        config = yaml.safe_load(config_file.read_text())

        assert "som1" in config["modules"]
        assert "som2" in config["modules"]
        assert config["modules"]["som1"]["host"] == "10.0.0.100"
        assert config["modules"]["som2"]["host"] == "10.0.0.101"

    def test_init_sets_defaults(self, tmp_path):
        """Init should set default values for backup_dir and max_backups."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        runner.invoke(
            main,
            ["init", "--config-dir", str(config_dir)],
            input="\n192.168.1.50\nroot\n\n",  # module, host, user, no more modules
        )

        config_file = config_dir / "config.yaml"
        config = yaml.safe_load(config_file.read_text())

        assert config["backup_dir"] == "/opt/satdeploy/backups"
        assert config["max_backups"] == 10
        assert "example_app" in config["apps"]
        assert config["apps"]["example_app"]["service"] is None

    def test_init_warns_if_config_exists(self, tmp_path):
        """Init should warn if config already exists."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("target:\n  host: old\n")

        result = runner.invoke(
            main,
            ["init", "--config-dir", str(config_dir)],
            input="n\n",  # Don't overwrite
        )

        # Should ask about overwriting
        assert "exist" in result.output.lower() or "overwrite" in result.output.lower()

    def test_init_can_overwrite_existing_config(self, tmp_path):
        """Init should overwrite config if user confirms."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text("modules:\n  old:\n    host: old\n    user: old\n")

        result = runner.invoke(
            main,
            ["init", "--config-dir", str(config_dir)],
            input="y\n\n192.168.1.50\nroot\n\n",  # Overwrite, module, host, user, no more
        )

        config = yaml.safe_load((config_dir / "config.yaml").read_text())
        assert config["modules"]["default"]["host"] == "192.168.1.50"


class TestInitPolishedOutput:
    """Tests for polished CLI output formatting."""

    def test_init_success_shows_checkmark(self, tmp_path):
        """Init should show checkmark when config is saved."""
        runner = CliRunner()
        config_dir = tmp_path / ".satdeploy"

        result = runner.invoke(
            main,
            ["init", "--config-dir", str(config_dir)],
            input="\n192.168.1.50\nroot\n\n",  # module, host, user, no more modules
            color=True,
        )

        assert result.exit_code == 0
        assert SYMBOLS["check"] in result.output
