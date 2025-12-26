"""Tests for configuration module."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from satdeploy.config import Config, DEFAULT_CONFIG_DIR


class TestConfigPath:
    """Test config path resolution."""

    def test_default_config_dir_is_in_home(self):
        """Config directory should default to ~/.satdeploy."""
        assert DEFAULT_CONFIG_DIR == Path.home() / ".satdeploy"

    def test_config_file_path(self):
        """Config file should be config.yaml in config dir."""
        config = Config()
        assert config.config_path == DEFAULT_CONFIG_DIR / "config.yaml"


class TestConfigLoad:
    """Test loading configuration from file."""

    def test_load_nonexistent_config_returns_none(self, tmp_path):
        """Loading a non-existent config should return None."""
        config = Config(config_dir=tmp_path)
        assert config.load() is None

    def test_load_valid_config(self, tmp_path):
        """Should load a valid YAML config file."""
        config_file = tmp_path / "config.yaml"
        config_data = {
            "target": {"host": "192.168.1.50", "user": "root"},
            "backup_dir": "/opt/satdeploy/backups",
            "max_backups": 10,
            "apps": {
                "controller": {
                    "local": "./build/controller",
                    "remote": "/opt/disco/bin/controller",
                    "service": "controller.service",
                }
            },
        }
        config_file.write_text(yaml.dump(config_data))

        config = Config(config_dir=tmp_path)
        loaded = config.load()

        assert loaded is not None
        assert loaded["target"]["host"] == "192.168.1.50"
        assert loaded["target"]["user"] == "root"
        assert loaded["apps"]["controller"]["service"] == "controller.service"

    def test_load_invalid_yaml_raises_error(self, tmp_path):
        """Loading invalid YAML should raise an error."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("invalid: yaml: content: [")

        config = Config(config_dir=tmp_path)
        with pytest.raises(yaml.YAMLError):
            config.load()


class TestConfigSave:
    """Test saving configuration to file."""

    def test_save_creates_config_dir(self, tmp_path):
        """Saving should create the config directory if it doesn't exist."""
        config_dir = tmp_path / "new_dir"
        config = Config(config_dir=config_dir)

        config.save({"target": {"host": "192.168.1.50"}})

        assert config_dir.exists()
        assert (config_dir / "config.yaml").exists()

    def test_save_writes_valid_yaml(self, tmp_path):
        """Saved config should be valid YAML."""
        config = Config(config_dir=tmp_path)
        data = {
            "target": {"host": "192.168.1.50", "user": "root"},
            "backup_dir": "/opt/satdeploy/backups",
        }

        config.save(data)

        loaded = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert loaded == data


class TestConfigValidation:
    """Test configuration validation."""

    def test_validate_missing_target_fails(self):
        """Config without target should fail validation."""
        config = Config()
        errors = config.validate({"apps": {}})
        assert "target" in errors

    def test_validate_missing_host_fails(self):
        """Config without target.host should fail validation."""
        config = Config()
        errors = config.validate({"target": {"user": "root"}, "apps": {}})
        assert "target.host" in errors

    def test_validate_valid_config_passes(self):
        """Valid config should pass validation."""
        config = Config()
        data = {
            "target": {"host": "192.168.1.50", "user": "root"},
            "backup_dir": "/opt/satdeploy/backups",
            "max_backups": 10,
            "apps": {},
        }
        errors = config.validate(data)
        assert errors == []

    def test_validate_app_missing_local_fails(self):
        """App without local path should fail validation."""
        config = Config()
        data = {
            "target": {"host": "192.168.1.50", "user": "root"},
            "apps": {"myapp": {"remote": "/usr/bin/myapp"}},
        }
        errors = config.validate(data)
        assert any("local" in e for e in errors)

    def test_validate_app_missing_remote_fails(self):
        """App without remote path should fail validation."""
        config = Config()
        data = {
            "target": {"host": "192.168.1.50", "user": "root"},
            "apps": {"myapp": {"local": "./build/myapp"}},
        }
        errors = config.validate(data)
        assert any("remote" in e for e in errors)


class TestConfigGetApp:
    """Test getting app configuration."""

    def test_get_app_returns_app_config(self, tmp_path):
        """Should return config for a specific app."""
        config_file = tmp_path / "config.yaml"
        config_data = {
            "target": {"host": "192.168.1.50", "user": "root"},
            "apps": {
                "controller": {
                    "local": "./build/controller",
                    "remote": "/opt/disco/bin/controller",
                    "service": "controller.service",
                }
            },
        }
        config_file.write_text(yaml.dump(config_data))

        config = Config(config_dir=tmp_path)
        config.load()
        app = config.get_app("controller")

        assert app["local"] == "./build/controller"
        assert app["remote"] == "/opt/disco/bin/controller"

    def test_get_nonexistent_app_returns_none(self, tmp_path):
        """Getting a non-existent app should return None."""
        config_file = tmp_path / "config.yaml"
        config_data = {
            "target": {"host": "192.168.1.50", "user": "root"},
            "apps": {},
        }
        config_file.write_text(yaml.dump(config_data))

        config = Config(config_dir=tmp_path)
        config.load()
        assert config.get_app("nonexistent") is None
