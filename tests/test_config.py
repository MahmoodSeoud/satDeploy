"""Tests for configuration module."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from satdeploy.config import Config, DEFAULT_CONFIG_DIR, ModuleConfig, AppConfig


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


class TestModuleConfig:
    """Test ModuleConfig dataclass."""

    def test_module_config_holds_module_settings(self):
        """ModuleConfig should hold all module-specific settings."""
        module = ModuleConfig(
            name="som1",
            host="192.168.1.10",
            user="root",
            csp_addr=5421,
            netmask=8,
            interface=0,
            baudrate=100000,
            vmem_path="/home/root/a53vmem",
        )

        assert module.name == "som1"
        assert module.host == "192.168.1.10"
        assert module.user == "root"
        assert module.csp_addr == 5421
        assert module.netmask == 8
        assert module.interface == 0
        assert module.baudrate == 100000
        assert module.vmem_path == "/home/root/a53vmem"


class TestAppConfig:
    """Test AppConfig dataclass."""

    def test_app_config_holds_app_settings(self):
        """AppConfig should hold all app-specific settings."""
        app = AppConfig(
            name="a53-app-sys-manager",
            local="./build/a53-app-sys-manager",
            remote="/usr/bin/a53-app-sys-manager",
            service="a53-app-sys-manager.service",
            service_template="[Unit]\nDescription=Test",
            vmem_dir="/home/root/a53vmem",
        )

        assert app.name == "a53-app-sys-manager"
        assert app.local == "./build/a53-app-sys-manager"
        assert app.remote == "/usr/bin/a53-app-sys-manager"
        assert app.service == "a53-app-sys-manager.service"
        assert app.service_template == "[Unit]\nDescription=Test"
        assert app.vmem_dir == "/home/root/a53vmem"

    def test_app_config_optional_fields_can_be_none(self):
        """AppConfig optional fields should allow None."""
        app = AppConfig(
            name="upload_client",
            local="./build/upload_client",
            remote="/usr/bin/upload_client",
            service=None,
            service_template=None,
            vmem_dir=None,
        )

        assert app.service is None
        assert app.service_template is None
        assert app.vmem_dir is None


class TestGetModules:
    """Test get_modules() and get_module() methods."""

    def test_get_modules_returns_all_modules(self, tmp_path):
        """get_modules() should return all configured modules."""
        config_file = tmp_path / "config.yaml"
        config_data = {
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
            "apps": {},
        }
        config_file.write_text(yaml.dump(config_data))

        config = Config(config_dir=tmp_path)
        config.load()
        modules = config.get_modules()

        assert len(modules) == 2
        assert "som1" in modules
        assert "som2" in modules
        assert isinstance(modules["som1"], ModuleConfig)

    def test_get_module_returns_module_with_appsys_settings(self, tmp_path):
        """get_module() should return ModuleConfig with inherited appsys settings."""
        config_file = tmp_path / "config.yaml"
        config_data = {
            "modules": {
                "som1": {
                    "host": "192.168.1.10",
                    "user": "root",
                    "csp_addr": 5421,
                },
            },
            "appsys": {
                "netmask": 8,
                "interface": 0,
                "baudrate": 100000,
                "vmem_path": "/home/root/a53vmem",
            },
            "apps": {},
        }
        config_file.write_text(yaml.dump(config_data))

        config = Config(config_dir=tmp_path)
        config.load()
        module = config.get_module("som1")

        assert module.name == "som1"
        assert module.host == "192.168.1.10"
        assert module.user == "root"
        assert module.csp_addr == 5421
        assert module.netmask == 8
        assert module.interface == 0
        assert module.baudrate == 100000
        assert module.vmem_path == "/home/root/a53vmem"

    def test_get_module_unknown_raises_keyerror(self, tmp_path):
        """get_module() should raise KeyError for unknown module."""
        config_file = tmp_path / "config.yaml"
        config_data = {
            "modules": {
                "som1": {
                    "host": "192.168.1.10",
                    "user": "root",
                    "csp_addr": 5421,
                },
            },
            "appsys": {
                "netmask": 8,
                "interface": 0,
                "baudrate": 100000,
                "vmem_path": "/home/root/a53vmem",
            },
            "apps": {},
        }
        config_file.write_text(yaml.dump(config_data))

        config = Config(config_dir=tmp_path)
        config.load()

        with pytest.raises(KeyError):
            config.get_module("unknown")
