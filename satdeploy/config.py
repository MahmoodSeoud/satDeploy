"""Configuration loading and validation for satdeploy."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class ModuleConfig:
    """Configuration for a deployment target module.

    Supports three transport types:
    - "ssh":   Traditional SSH/SFTP (requires host, user)
    - "csp":   CSP/DTP over ZMQ/CAN/KISS (requires zmq_endpoint, agent_node)
    - "local": Local filesystem target (requires target_dir) — used by
               `satdeploy demo` and for deploying to chroots/mounted rootfs
    """

    name: str
    transport: str  # "ssh", "csp", or "local"

    # SSH transport fields
    host: Optional[str] = None
    user: Optional[str] = None

    # Local transport fields
    target_dir: Optional[str] = None

    # CSP transport fields
    zmq_endpoint: Optional[str] = None
    agent_node: Optional[int] = None
    appsys_node: Optional[int] = None
    ground_node: int = 40  # Default ground station node address
    zmq_pub_port: int = 9600  # zmqproxy subscribe port (TX)
    zmq_sub_port: int = 9601  # zmqproxy publish port (RX)

    # DTP transfer tuning
    dtp_mtu: int = 1024        # Max transmission unit (bytes)
    dtp_throughput: int = 10000000  # Target throughput (bytes/s)
    dtp_timeout: int = 60      # Transfer timeout (seconds)

    # Common fields
    csp_addr: int = 0
    netmask: int = 0
    interface: int = 0
    baudrate: int = 0
    vmem_path: str = ""

    # Per-app CSP node addresses (app_name -> run_node)
    app_nodes: dict[str, int] | None = None

    def get_run_node(self, app_name: str) -> int | None:
        """Get the CSP run_node for an app on this module."""
        if self.app_nodes is None:
            return None
        return self.app_nodes.get(app_name)


@dataclass
class AppConfig:
    """Configuration for a deployable application."""

    name: str
    local: str
    remote: str
    service: str | None = None
    service_template: str | None = None
    vmem_dir: str | None = None

    # CSP-specific fields for libparam control
    param: str | None = None  # libparam name (e.g., "mng_dipp")

DEFAULT_CONFIG_DIR = Path.home() / ".satdeploy"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.yaml"


class Config:
    """Handles loading, saving, and validating satdeploy configuration.

    Expects a flat config format with target settings at the top level:

        name: som1
        transport: csp
        zmq_endpoint: tcp://localhost:9600
        agent_node: 5425
        ...
        apps:
          dipp: ...
    """

    def __init__(self, config_path: Optional[Path] = None):
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_FILE
        self._data: Optional[dict] = None

    @property
    def config_path(self) -> Path:
        """Path to the config YAML file."""
        return self._config_path

    @property
    def history_path(self) -> Path:
        """Path to the history database, derived from the config filename.

        ~/.satdeploy/config.yaml      -> ~/.satdeploy/history.db
        ~/.satdeploy/.demo-config.yaml -> ~/.satdeploy/.demo-history.db
        """
        stem = self._config_path.stem  # "config" or ".demo-config"
        if stem.endswith("-config"):
            db_name = stem[:-7] + "-history.db"  # ".demo-config" -> ".demo-history.db"
        else:
            db_name = "history.db"
        return self._config_path.parent / db_name

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

        # Merge APM-style `defaults:` block into top level so both formats work.
        # Top-level fields take precedence over defaults.
        if self._data and "defaults" in self._data:
            defaults = self._data.pop("defaults")
            if isinstance(defaults, dict):
                for key, value in defaults.items():
                    if key not in self._data:
                        self._data[key] = value

        return self._data

    def save(self, data: dict) -> None:
        """Save configuration to disk.

        Creates the config directory if it doesn't exist.

        Args:
            data: The configuration dictionary to save.
        """
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
        self._data = data

    def validate(self, data: dict) -> list[str]:
        """Validate flat configuration data.

        Args:
            data: The configuration dictionary to validate.

        Returns:
            A list of error messages. Empty list if valid.
        """
        errors = []

        transport = data.get("transport", "ssh")

        if transport == "ssh":
            if "host" not in data:
                errors.append("host")
            if "user" not in data:
                errors.append("user")
        elif transport == "csp":
            if "zmq_endpoint" not in data:
                errors.append("zmq_endpoint")
            if "agent_node" not in data:
                errors.append("agent_node")
        elif transport == "local":
            if "target_dir" not in data:
                errors.append("target_dir")
        else:
            errors.append(f"unknown transport: {transport}")

        apps = data.get("apps", {})
        for app_name, app_config in apps.items():
            if "local" not in app_config:
                errors.append(f"apps.{app_name}.local")
            if "remote" not in app_config:
                errors.append(f"apps.{app_name}.remote")

        return errors

    @property
    def module_name(self) -> str:
        """Get the target name from config (defaults to 'default')."""
        if self._data is None:
            return "default"
        return self._data.get("name", "default")

    def get_target(self) -> ModuleConfig:
        """Build a ModuleConfig from top-level flat config fields.

        Returns:
            ModuleConfig with settings from the flat config.
        """
        if self._data is None:
            raise RuntimeError("Config not loaded")

        name = self._data.get("name", "default")
        transport = self._data.get("transport", "ssh")
        appsys = self._data.get("appsys", {})

        return ModuleConfig(
            name=name,
            transport=transport,
            # SSH fields
            host=self._data.get("host"),
            user=self._data.get("user"),
            # Local transport fields
            target_dir=self._data.get("target_dir"),
            # CSP fields
            zmq_endpoint=self._data.get("zmq_endpoint"),
            agent_node=self._data.get("agent_node"),
            appsys_node=self._data.get("appsys_node"),
            ground_node=self._data.get("ground_node", 40),
            zmq_pub_port=self._data.get("zmq_pub_port", 9600),
            zmq_sub_port=self._data.get("zmq_sub_port", 9601),
            # Common fields
            csp_addr=self._data.get("csp_addr", 0),
            netmask=appsys.get("netmask", 0),
            interface=appsys.get("interface", 0),
            baudrate=appsys.get("baudrate", 0),
            vmem_path=appsys.get("vmem_path", ""),
            # Per-app node addresses
            app_nodes=self._data.get("app_nodes"),
        )

    def get_modules(self) -> dict[str, ModuleConfig]:
        """Get all configured modules.

        For flat config, returns a single-entry dict keyed by module_name.

        Returns:
            Dictionary mapping module name to ModuleConfig.
        """
        if self._data is None:
            return {}

        return {self.module_name: self.get_target()}

    def get_module(self, name: str) -> ModuleConfig:
        """Get target module configuration.

        Transitional: ignores name parameter, returns the single target.

        Returns:
            ModuleConfig with target settings.
        """
        return self.get_target()

    def get_app(self, name: str) -> Optional[AppConfig]:
        """Get configuration for a specific app.

        Args:
            name: The app name.

        Returns:
            The AppConfig, or None if not found.
        """
        if self._data is None:
            return None

        apps = self._data.get("apps", {})
        if name not in apps:
            return None

        app_data = apps[name]
        return AppConfig(
            name=name,
            local=app_data["local"],
            remote=app_data["remote"],
            service=app_data.get("service"),
            service_template=app_data.get("service_template"),
            vmem_dir=app_data.get("vmem_dir"),
            param=app_data.get("param"),
        )

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

    def get_all_app_names(self) -> list[str]:
        """Get names of all configured apps.

        Returns:
            List of app names.
        """
        if self._data is None:
            return []
        return list(self._data.get("apps", {}).keys())

    def get_appsys(self) -> dict:
        """Get appsys network settings.

        Returns:
            Dictionary with appsys settings (netmask, interface, etc.).
        """
        if self._data is None:
            return {}
        return self._data.get("appsys", {})
