"""Configuration loading and validation for satdeploy."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class ModuleConfig:
    """Configuration for a deployment target module."""

    name: str
    host: str
    user: str
    csp_addr: int
    netmask: int
    interface: int
    baudrate: int
    vmem_path: str


@dataclass
class AppConfig:
    """Configuration for a deployable application."""

    name: str
    local: str
    remote: str
    service: str | None
    service_template: str | None
    vmem_dir: str | None

DEFAULT_CONFIG_DIR = Path.home() / ".satdeploy"


class Config:
    """Handles loading, saving, and validating satdeploy configuration."""

    def __init__(self, config_dir: Optional[Path] = None):
        self._config_dir = config_dir or DEFAULT_CONFIG_DIR
        self._data: Optional[dict] = None

    @property
    def config_path(self) -> Path:
        """Path to the config.yaml file."""
        return self._config_dir / "config.yaml"

    def load(self) -> Optional[dict]:
        """Load configuration from disk.

        Returns:
            The configuration dictionary, or None if file doesn't exist.

        Raises:
            yaml.YAMLError: If the config file contains invalid YAML.
        """
        if not self.config_path.exists():
            return None

        with open(self.config_path) as f:
            self._data = yaml.safe_load(f)
        return self._data

    def save(self, data: dict) -> None:
        """Save configuration to disk.

        Creates the config directory if it doesn't exist.

        Args:
            data: The configuration dictionary to save.
        """
        self._config_dir.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
        self._data = data

    def validate(self, data: dict) -> list[str]:
        """Validate configuration data.

        Args:
            data: The configuration dictionary to validate.

        Returns:
            A list of error messages. Empty list if valid.
        """
        errors = []

        if "target" not in data:
            errors.append("target")
            return errors

        target = data["target"]
        if "host" not in target:
            errors.append("target.host")
        if "user" not in target:
            errors.append("target.user")

        apps = data.get("apps", {})
        for app_name, app_config in apps.items():
            if "local" not in app_config:
                errors.append(f"apps.{app_name}.local")
            if "remote" not in app_config:
                errors.append(f"apps.{app_name}.remote")

        return errors

    def get_app(self, name: str) -> Optional[dict]:
        """Get configuration for a specific app.

        Args:
            name: The app name.

        Returns:
            The app configuration, or None if not found.
        """
        if self._data is None:
            return None
        return self._data.get("apps", {}).get(name)

    @property
    def target(self) -> Optional[dict]:
        """Get target configuration."""
        if self._data is None:
            return None
        return self._data.get("target")

    @property
    def apps(self) -> dict:
        """Get all app configurations."""
        if self._data is None:
            return {}
        return self._data.get("apps", {})

    @property
    def backup_dir(self) -> str:
        """Get backup directory path."""
        if self._data is None:
            return "/opt/satdeploy/backups"
        return self._data.get("backup_dir", "/opt/satdeploy/backups")

    @property
    def max_backups(self) -> int:
        """Get maximum number of backups to keep per app."""
        if self._data is None:
            return 10
        return self._data.get("max_backups", 10)

    def get_modules(self) -> dict[str, ModuleConfig]:
        """Get all configured modules.

        Returns:
            Dictionary mapping module names to ModuleConfig objects.
        """
        if self._data is None:
            return {}

        modules_data = self._data.get("modules", {})
        appsys = self._data.get("appsys", {})

        result = {}
        for name, mod in modules_data.items():
            result[name] = ModuleConfig(
                name=name,
                host=mod["host"],
                user=mod["user"],
                csp_addr=mod["csp_addr"],
                netmask=appsys.get("netmask", 0),
                interface=appsys.get("interface", 0),
                baudrate=appsys.get("baudrate", 0),
                vmem_path=appsys.get("vmem_path", ""),
            )
        return result

    def get_module(self, name: str) -> ModuleConfig:
        """Get configuration for a specific module.

        Args:
            name: The module name.

        Returns:
            ModuleConfig with module and inherited appsys settings.

        Raises:
            KeyError: If module name is not found.
        """
        modules = self.get_modules()
        if name not in modules:
            raise KeyError(f"Module '{name}' not found")
        return modules[name]
