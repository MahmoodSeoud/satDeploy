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

    # Validation: shell command run on the target by `satdeploy validate`.
    # Optional — apps without it cannot be validated and therefore cannot
    # pass `push --requires-validated`. Interpreted by the target shell.
    validate_command: str | None = None
    # Hard timeout (seconds) for the validate run. Default 300s; design-doc
    # Phase-0 thesis metric #3 wants validate to be a binary signal, not
    # something that hangs forever in CI.
    validate_timeout_seconds: int = 300

DEFAULT_CONFIG_DIR = Path.home() / ".satdeploy"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.yaml"

# Top-level keys that describe transport for a single target. When found at
# the top level of a flat-format config, they are lifted into the synthesized
# targets dict on load().
_TARGET_FIELDS = (
    "transport",
    "host",
    "user",
    "target_dir",
    "zmq_endpoint",
    "agent_node",
    "appsys_node",
    "ground_node",
    "zmq_pub_port",
    "zmq_sub_port",
    "csp_addr",
    "appsys",
    "app_nodes",
    "backup_dir",
)


class Config:
    """Handles loading, saving, and validating satdeploy configuration.

    Supports two YAML shapes, normalized to the same internal form on load():

    Flat (legacy, single-target) — top-level `name` is the target name and
    transport fields sit at the top level:

        name: som1
        transport: csp
        zmq_endpoint: tcp://localhost:9600
        agent_node: 5425
        apps:
          dipp: ...

    Multi-target — transport config lives under `targets:` keyed by name,
    with `default_target:` selecting which target `--target`-less commands use:

        default_target: som1
        targets:
          som1:
            transport: local
            target_dir: /tmp/som1
          som2:
            transport: local
            target_dir: /tmp/som2
        apps:
          dipp: ...

    After load(), `self._targets` always holds {name -> target_dict} and
    `self._default_target` names the one returned by `get_target()` (no name).
    """

    def __init__(self, config_path: Optional[Path] = None):
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_FILE
        self._data: Optional[dict] = None
        self._targets: dict[str, dict] = {}
        self._default_target: Optional[str] = None

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
        """Load configuration from disk and normalize target layout.

        Returns:
            The raw configuration dictionary, or None if file doesn't exist.

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

        self._normalize_targets()
        return self._data

    def _normalize_targets(self) -> None:
        """Populate self._targets and self._default_target from self._data.

        Flat format → single-entry targets dict keyed by `name` (or "default").
        Multi-target format → uses the `targets:` block directly.
        """
        self._targets = {}
        self._default_target = None
        if self._data is None:
            return

        data = self._data
        if "targets" in data and isinstance(data["targets"], dict):
            self._targets = {
                name: dict(td or {})
                for name, td in data["targets"].items()
            }
            self._default_target = data.get("default_target")
            if self._default_target is None and self._targets:
                self._default_target = next(iter(self._targets))
        else:
            name = data.get("name", "default")
            target_fields = {key: data[key] for key in _TARGET_FIELDS if key in data}
            self._targets = {name: target_fields}
            self._default_target = name

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
        self._normalize_targets()

    def validate(self, data: dict) -> list[str]:
        """Validate flat configuration data.

        Args:
            data: The configuration dictionary to validate.

        Returns:
            A list of error messages. Empty list if valid.
        """
        errors = []

        if "targets" in data and isinstance(data["targets"], dict):
            for tname, tdata in data["targets"].items():
                tdata = tdata or {}
                errors.extend(
                    f"targets.{tname}.{e}" for e in self._validate_transport(tdata)
                )
        else:
            errors.extend(self._validate_transport(data))

        apps = data.get("apps", {})
        for app_name, app_config in apps.items():
            if "local" not in app_config:
                errors.append(f"apps.{app_name}.local")
            if "remote" not in app_config:
                errors.append(f"apps.{app_name}.remote")

        return errors

    def _validate_transport(self, data: dict) -> list[str]:
        """Validate transport-specific required fields for a single target."""
        errors: list[str] = []
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
        return errors

    @property
    def module_name(self) -> str:
        """Get the default target name (defaults to 'default')."""
        return self._default_target or "default"

    @property
    def target_names(self) -> list[str]:
        """Names of all configured targets, preserving insertion order."""
        return list(self._targets.keys())

    def get_target(self, name: Optional[str] = None) -> ModuleConfig:
        """Build a ModuleConfig for a named target (or the default).

        Args:
            name: Target name. If None, returns the default target (first
                target in multi-target config, or `name:` in flat config).

        Returns:
            ModuleConfig for the resolved target.

        Raises:
            RuntimeError: If config not loaded.
            KeyError: If `name` is not in the configured targets.
        """
        if self._data is None:
            raise RuntimeError("Config not loaded")

        resolved = name if name is not None else self._default_target
        if resolved is None or resolved not in self._targets:
            raise KeyError(
                f"Target '{resolved}' not in config "
                f"(available: {list(self._targets)})"
            )

        td = self._targets[resolved]
        appsys = td.get("appsys", {}) or {}

        return ModuleConfig(
            name=resolved,
            transport=td.get("transport", "ssh"),
            # SSH fields
            host=td.get("host"),
            user=td.get("user"),
            # Local transport fields
            target_dir=td.get("target_dir"),
            # CSP fields
            zmq_endpoint=td.get("zmq_endpoint"),
            agent_node=td.get("agent_node"),
            appsys_node=td.get("appsys_node"),
            ground_node=td.get("ground_node", 40),
            zmq_pub_port=td.get("zmq_pub_port", 9600),
            zmq_sub_port=td.get("zmq_sub_port", 9601),
            # Common fields
            csp_addr=td.get("csp_addr", 0),
            netmask=appsys.get("netmask", 0),
            interface=appsys.get("interface", 0),
            baudrate=appsys.get("baudrate", 0),
            vmem_path=appsys.get("vmem_path", ""),
            # Per-app node addresses
            app_nodes=td.get("app_nodes"),
        )

    def get_modules(self) -> dict[str, ModuleConfig]:
        """Get all configured targets as ModuleConfigs.

        Returns:
            Dictionary mapping target name to ModuleConfig.
        """
        return {name: self.get_target(name) for name in self._targets}

    def get_module(self, name: str) -> ModuleConfig:
        """Deprecated alias for `get_target(name)`.

        Kept for callers still using the pre-R1 name. Prefer `get_target()`.
        """
        return self.get_target(name)

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
            validate_command=app_data.get("validate_command"),
            validate_timeout_seconds=app_data.get("validate_timeout_seconds", 300),
        )

    @property
    def apps(self) -> dict:
        """Get all app configurations."""
        if self._data is None:
            return {}
        return self._data.get("apps", {})

    def get_backup_dir(self, target_name: Optional[str] = None) -> str:
        """Get backup directory for a specific target.

        Per-target `backup_dir` wins over top-level `backup_dir`. For local
        transport without explicit setting, derives `{target_dir}/.satdeploy-backups`
        so the default lives next to the target (writable without root).
        """
        if self._data is None:
            return "/opt/satdeploy/backups"

        resolved = target_name if target_name is not None else self._default_target
        td = self._targets.get(resolved, {}) if resolved else {}

        explicit = td.get("backup_dir") or self._data.get("backup_dir")
        if explicit:
            return explicit

        if td.get("transport") == "local":
            target_dir = td.get("target_dir", "")
            if target_dir:
                return str(Path(target_dir).expanduser() / ".satdeploy-backups")

        return "/opt/satdeploy/backups"

    @property
    def backup_dir(self) -> str:
        """Backup directory for the default target (see `get_backup_dir`)."""
        return self.get_backup_dir()

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

    def get_appsys(self, target_name: Optional[str] = None) -> dict:
        """Get appsys network settings for a target.

        Returns:
            Dictionary with appsys settings (netmask, interface, etc.).
        """
        if self._data is None:
            return {}
        resolved = target_name if target_name is not None else self._default_target
        td = self._targets.get(resolved, {}) if resolved else {}
        return td.get("appsys", {}) or self._data.get("appsys", {}) or {}

    def get_require_validated(self, target_name: Optional[str] = None) -> bool:
        """Whether `push` should default-on the `--requires-validated` gate
        for this target.

        Per-target `push.require_validated` wins over top-level `push.
        require_validated`. Default is False (the CLI flag is opt-in for
        general targets and config-on for flight targets — see design-doc
        Open Question #0). Returning True here just sets the *default*; the
        explicit `--requires-validated` flag still wins, and there is no
        config knob today to *suppress* a flag-set gate (paternalism risk
        revisited with first pilot).
        """
        if self._data is None:
            return False
        resolved = target_name if target_name is not None else self._default_target
        td = self._targets.get(resolved, {}) if resolved else {}
        per_target = (td.get("push") or {}).get("require_validated")
        if per_target is not None:
            return bool(per_target)
        top_level = (self._data.get("push") or {}).get("require_validated")
        return bool(top_level)
