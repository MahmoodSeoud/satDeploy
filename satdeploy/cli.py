"""CLI entry point for satdeploy."""

import os
import subprocess
from datetime import datetime
from pathlib import Path

import click
from click.shell_completion import CompletionItem

from satdeploy.config import DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_FILE, Config, ModuleConfig, AppConfig
from satdeploy.dependencies import DependencyResolver
from satdeploy.deployer import Deployer
from satdeploy.hash import compute_file_hash
from satdeploy.provenance import capture_provenance, is_dirty, resolve_provenance
from satdeploy.history import DeploymentRecord, History
from satdeploy.output import success, warning, step, SYMBOLS, SatDeployError, ColoredGroup
from satdeploy.services import ServiceManager, ServiceStatus
from satdeploy.ssh import SSHClient, SSHError
from satdeploy.templates import render_service_template, compute_service_hash
from satdeploy.transport import Transport, SSHTransport, CSPTransport, LocalTransport, TransportError
from satdeploy import demo as demo_module


def get_transport(
    module: ModuleConfig,
    backup_dir: str,
    apps: dict[str, dict] | None = None,
) -> Transport:
    """Create the appropriate transport for a module.

    Args:
        module: The module configuration.
        backup_dir: Remote backup directory path.
        apps: Dictionary of app configs (used by SSH transport for status queries).

    Returns:
        Transport instance (SSHTransport or CSPTransport).

    Raises:
        ValueError: If transport type is unknown.
    """
    if module.transport == "ssh":
        return SSHTransport(
            host=module.host,
            user=module.user,
            backup_dir=backup_dir,
            apps=apps,
        )
    elif module.transport == "csp":
        return CSPTransport(
            zmq_endpoint=module.zmq_endpoint,
            agent_node=module.agent_node,
            ground_node=module.ground_node,
            backup_dir=backup_dir,
            zmq_pub_port=module.zmq_pub_port,
            zmq_sub_port=module.zmq_sub_port,
            dtp_mtu=module.dtp_mtu,
            dtp_throughput=module.dtp_throughput,
            dtp_timeout=module.dtp_timeout,
        )
    elif module.transport == "local":
        return LocalTransport(
            target_dir=module.target_dir or "",
            backup_dir=backup_dir,
            apps=apps,
        )
    else:
        raise ValueError(f"Unknown transport type: {module.transport}")


def get_history(db_path: Path) -> History:
    """Get or create the history database."""
    history = History(db_path)
    history.init_db()
    return history


def build_provenance_map(history: History, app: str) -> dict[str, str]:
    """Build a mapping from file hash to git provenance string.

    Args:
        history: The history database.
        app: The app name to look up.

    Returns:
        Dict mapping file_hash to git_hash provenance string.
    """
    prov_map = {}
    for rec in history.get_history(app):
        if rec.file_hash and rec.git_hash and rec.file_hash not in prov_map:
            prov_map[rec.file_hash] = rec.git_hash
    return prov_map


def format_iso_timestamp(iso_str: str | None) -> str:
    """Format an ISO timestamp string to human-readable format.

    Args:
        iso_str: ISO format timestamp (e.g., "2024-01-15T14:30:22")

    Returns:
        Formatted string like "2024-01-15 14:30:22" or "-" if invalid.
    """
    if not iso_str:
        return "-"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "-"


def get_services_to_manage(
    config: Config,
    app: str,
    service: str | None,
) -> list[tuple[str, str]]:
    """Get list of services to stop/start for an app deployment.

    For apps with a restart list (libraries), returns those services.
    For apps with dependencies, returns the dependency chain.
    For standalone apps, returns just that app's service.

    Args:
        config: The loaded configuration.
        app: The app being deployed/rolled back.
        service: The app's service name (None for libraries).

    Returns:
        List of (app_name, service_name) tuples in stop order.

    Raises:
        SatDeployError: If cyclic dependencies are detected.
    """
    resolver = DependencyResolver(config.apps)

    if resolver.has_cycle():
        raise SatDeployError("Cyclic dependency detected in config")

    # For libraries with restart list, use that
    restart_apps = resolver.get_restart_apps(app)
    if restart_apps:
        services = []
        for restart_app in restart_apps:
            restart_config = config.get_app(restart_app)
            if restart_config and restart_config.service:
                services.append((restart_app, restart_config.service))
        return services

    # For services with dependencies, get the full stop order
    if service:
        stop_order = resolver.get_stop_order(app)
        services = []
        for dep_app in stop_order:
            dep_config = config.get_app(dep_app)
            if dep_config and dep_config.service:
                services.append((dep_app, dep_config.service))
        return services

    return []


def get_app_config_or_error(config: Config, app: str):
    """Get app configuration or raise ClickException if not found.

    Args:
        config: The loaded configuration.
        app: The app name to look up.

    Returns:
        The AppConfig object.

    Raises:
        SatDeployError: If the app is not found in config.
    """
    app_config = config.get_app(app)
    if app_config is None:
        raise SatDeployError(
            f"App '{app}' not found in config. Check your config.yaml."
        )
    return app_config


class StepCounter:
    """Simple counter for step-by-step progress output."""

    def __init__(self, total: int):
        self.current = 0
        self.total = total

    def next(self, message: str) -> None:
        self.current += 1
        click.echo(step(self.current, self.total, message))


def stop_services(
    service_manager: ServiceManager,
    services: list[tuple[str, str]],
    counter: StepCounter,
) -> None:
    """Stop services in order with progress output."""
    for svc_app, svc_name in services:
        counter.next(f"Stopping {svc_app} ({svc_name})")
        if not service_manager.stop(svc_name):
            click.echo(warning(f"Service {svc_name} not found - skipping stop"))


def start_services(
    service_manager: ServiceManager,
    services: list[tuple[str, str]],
    counter: StepCounter,
) -> None:
    """Start services in reverse order with health checks."""
    for svc_app, svc_name in reversed(services):
        counter.next(f"Starting {svc_app} ({svc_name})")
        if not service_manager.start(svc_name):
            click.echo(warning(f"Service {svc_name} not found - skipping start"))
            continue
        if service_manager.is_healthy(svc_name):
            click.echo(success(f"Health check passed for {svc_app}"))
        else:
            click.echo(warning(f"Health check failed for {svc_app}"))


def sync_service_file(
    ssh: SSHClient,
    service_manager: ServiceManager,
    app_config: AppConfig,
    module_config: ModuleConfig,
    counter: StepCounter | None = None,
) -> str | None:
    """Sync service file to remote if needed.

    Renders the service template, compares with remote, and uploads if different.

    Args:
        ssh: The SSH client.
        service_manager: The service manager.
        app_config: The app configuration with service_template.
        module_config: The module configuration for template rendering.
        counter: Optional step counter for progress output.

    Returns:
        The service hash if synced, None if no template defined.
    """
    if not app_config.service_template or not app_config.service:
        return None

    # Render template with module values
    rendered = render_service_template(app_config.service_template, module_config)
    local_hash = compute_service_hash(rendered)

    # Check remote service file
    service_path = f"/etc/systemd/system/{app_config.service}"
    remote_content = ssh.read_file(service_path)

    needs_sync = True
    if remote_content is not None:
        remote_hash = compute_service_hash(remote_content)
        needs_sync = local_hash != remote_hash

    if counter:
        counter.next(f"Syncing service file ({app_config.service})")

    if not needs_sync:
        click.echo(f"                Service file unchanged")
        return local_hash

    # Service file missing or changed - upload it
    ssh.write_file_sudo(service_path, rendered)
    service_manager.daemon_reload()
    service_manager.enable(app_config.service)

    if remote_content is None:
        click.echo(success(f"Service file created"))
    else:
        click.echo(success(f"Service file updated"))

    return local_hash


def config_option(f):
    """Shared --config option pointing to the config YAML file."""
    return click.option(
        "--config",
        "config_path",
        type=click.Path(path_type=Path),
        default=None,
        envvar="SATDEPLOY_CONFIG",
        help="Config file (default: ~/.satdeploy/config.yaml)",
    )(f)


def load_config(config_path: Path | None) -> Config:
    """Load and validate config, raising SatDeployError on problems."""
    config = Config(config_path=config_path)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. "
            "Run 'satdeploy init' first."
        )

    errors = config.validate(config._data)
    if errors:
        raise SatDeployError(
            f"Invalid config at {config.config_path}: "
            f"missing fields: {', '.join(errors)}"
        )

    return config


def node_option(f):
    """Shared -n/--node option for targeting a specific CSP node."""
    return click.option(
        "-n", "--node",
        "node_override",
        type=int,
        default=None,
        help="Target CSP node (overrides agent_node from config)",
    )(f)


class AppNameType(click.ParamType):
    """Click parameter type with shell completion for app names from config."""

    name = "app"

    def shell_complete(self, ctx, param, incomplete):
        """Return completions for app names."""
        config_path = ctx.params.get("config_path")
        config = Config(config_path=config_path)
        if config.load() is None:
            return []
        names = config.get_all_app_names()
        return [
            CompletionItem(name)
            for name in names
            if name.startswith(incomplete)
        ]


APP_NAME = AppNameType()

def _detect_shell() -> str:
    """Detect the current shell."""
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return "zsh"
    elif "fish" in shell:
        return "fish"
    return "bash"


def _get_completion_path(shell: str) -> Path | None:
    """Get the system completion file path where shells auto-load from.

    zsh: site-functions dir on fpath (like gh, docker, brew)
    bash: /etc/bash_completion.d/ or ~/.local/share/bash-completion/completions/
    fish: ~/.config/fish/completions/
    """
    if shell == "zsh":
        # Check standard locations in priority order
        candidates = [
            Path("/usr/local/share/zsh/site-functions"),
            Path("/usr/share/zsh/site-functions"),
        ]
        # Also check Homebrew prefix
        brew_prefix = os.environ.get("HOMEBREW_PREFIX", "/opt/homebrew")
        candidates.insert(0, Path(brew_prefix) / "share" / "zsh" / "site-functions")

        for d in candidates:
            if d.is_dir() and os.access(d, os.W_OK):
                return d / "_satdeploy"
        # Fallback: user-local dir
        local_dir = Path.home() / ".local" / "share" / "zsh" / "site-functions"
        return local_dir / "_satdeploy"

    elif shell == "fish":
        return Path.home() / ".config" / "fish" / "completions" / "satdeploy.fish"

    else:  # bash
        user_dir = Path.home() / ".local" / "share" / "bash-completion" / "completions"
        return user_dir / "satdeploy"


def _generate_completion_script(shell: str) -> str:
    """Generate the full completion script by invoking Click's machinery."""
    env_var = "_SATDEPLOY_COMPLETE"
    if shell == "zsh":
        source_var = "zsh_source"
    elif shell == "fish":
        source_var = "fish_source"
    else:
        source_var = "bash_source"

    result = subprocess.run(
        ["satdeploy"],
        env={**os.environ, env_var: source_var},
        capture_output=True,
        text=True,
    )
    return result.stdout


def _install_completion() -> bool:
    """Install shell completion to the system completions directory.

    Writes a completion file where the shell auto-loads it (like gh, docker).
    Silent unless it actually installs something new.
    """
    shell = _detect_shell()
    comp_path = _get_completion_path(shell)
    if comp_path is None or comp_path.exists():
        return comp_path is not None

    script = _generate_completion_script(shell)
    if not script.strip():
        return False

    try:
        comp_path.parent.mkdir(parents=True, exist_ok=True)
        comp_path.write_text(script)
        return True
    except OSError:
        return False


@click.group(cls=ColoredGroup)
def main():
    """Deploy files to embedded Linux targets."""
    pass


@main.command()
@config_option
def init(config_path: Path | None):
    """Interactive setup, creates config.yaml."""
    config = Config(config_path=config_path)

    if config.config_path.exists():
        if not click.confirm("Config file already exists. Overwrite?"):
            click.echo("Aborted.")
            return

    click.echo(click.style("Setting up satdeploy configuration...", bold=True))
    click.echo("")

    name = click.prompt("Target name", default="default")
    transport = click.prompt(
        "Transport type",
        type=click.Choice(["ssh", "csp"]),
        default="ssh",
    )

    data = {"name": name, "transport": transport}

    if transport == "ssh":
        data["host"] = click.prompt("Target host (IP or hostname)")
        data["user"] = click.prompt("SSH user", default="root")
    else:  # csp
        data["zmq_endpoint"] = click.prompt(
            "ZMQ endpoint (zmqproxy host)",
            default="tcp://localhost:9600",
        )
        data["agent_node"] = click.prompt(
            "Agent CSP node",
            type=int,
            default=5425,
        )
        data["ground_node"] = click.prompt(
            "Ground CSP node",
            type=int,
            default=40,
        )
        data["appsys_node"] = click.prompt(
            "App-sys-manager CSP node",
            type=int,
            default=10,
        )

    data["backup_dir"] = "/opt/satdeploy/backups"
    data["max_backups"] = 10

    if transport == "ssh":
        data["apps"] = {
            "example_app": {
                "local": "/path/to/build/example_app",
                "remote": "/opt/app/bin/example_app",
                "service": "example_app.service",
            }
        }
    else:
        data["apps"] = {
            "example_app": {
                "local": "/path/to/build/example_app",
                "remote": "/opt/app/bin/example_app",
                "service": None,
                "param": None,
            }
        }

    config.save(data)

    # Sanity-check the generated config
    errors = config.validate(data)
    if errors:
        click.echo("")
        click.echo(warning(f"Config saved but has issues: {', '.join(errors)}"))
        click.echo(f"  Fix them in {config.config_path}")
        return

    click.echo("")
    click.echo(success(f"Config saved to {config.config_path}"))
    click.echo(f"  Edit local and remote paths in {config.config_path}")

    _install_completion()


@main.command(hidden=True)
@click.option("--install", is_flag=True, help="Install completion to system directory")
@click.option("--uninstall", is_flag=True, help="Remove installed completion")
def completion(install: bool, uninstall: bool):
    """Shell completion for satdeploy.

    Without flags, prints the completion script to stdout.
    With --install, writes it to the system completions directory
    (same place as gh, docker, brew — no rc file edits needed).
    """
    shell = _detect_shell()
    if uninstall:
        comp_path = _get_completion_path(shell)
        if comp_path and comp_path.exists():
            comp_path.unlink()
            click.echo(success(f"Removed {comp_path}"))
        else:
            click.echo("No completion file found.")
    elif install:
        _install_completion()
    else:
        script = _generate_completion_script(shell)
        click.echo(script)


@main.command()
@click.argument("apps", nargs=-1, type=APP_NAME)
@click.option("-a", "--all", "all_apps", is_flag=True, help="Deploy all apps")
@click.option(
    "-f", "--file", "--local",
    "local",
    type=click.Path(exists=False),
    default=None,
    help="Local file path (overrides config)",
)
@click.option(
    "-r", "--remote",
    "remote_override",
    type=click.Path(),
    default=None,
    help="Remote path on target (enables ad-hoc push without config entry)",
)
@click.option(
    "-F", "--force",
    is_flag=True,
    default=False,
    help="Force deploy even if same version",
)
@config_option
@node_option
def push(
    apps: tuple[str, ...],
    all_apps: bool,
    local: str | None,
    remote_override: str | None,
    force: bool,
    config_path: Path | None,
    node_override: int | None,
):
    """Deploy one or more apps to a target.

    APPS are the names of the applications to deploy.
    """
    config = load_config(config_path)

    # Validate flag combinations
    if remote_override and not local:
        raise SatDeployError("--remote requires --local")
    if remote_override and all_apps:
        raise SatDeployError("Cannot use --remote with --all")
    if remote_override and apps:
        raise SatDeployError("Cannot specify app name with --remote. Use --local and --remote without an app name for ad-hoc push.")

    # Ad-hoc mode: --local + --remote without app name
    adhoc_mode = bool(local and remote_override and not apps and not all_apps)
    adhoc_app_configs = {}  # app_name -> AppConfig for ad-hoc apps

    if adhoc_mode:
        local_path = os.path.expanduser(local)
        if not os.path.exists(local_path):
            raise SatDeployError(f"Local file not found: {local_path}")

        # Derive app name: basename, strip extension, dots to dashes
        basename = os.path.basename(remote_override)
        name_part, _ = os.path.splitext(basename)
        derived_name = name_part.replace(".", "-")

        # Avoid collision with configured apps
        if config.get_app(derived_name) is not None:
            derived_name = f"adhoc-{derived_name}"

        apps = (derived_name,)
        adhoc_app_configs[derived_name] = AppConfig(
            name=derived_name,
            local=local_path,
            remote=remote_override,
        )

        # Show ad-hoc warning
        file_size = os.path.getsize(local_path)
        size_str = f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024 else f"{file_size / (1024 * 1024):.1f} MB"
        click.echo(warning("Ad-hoc mode: pushing file directly without app configuration."))
        click.echo(f"  {SYMBOLS['bullet']} No service restart or dependency ordering")
        click.echo(f"  {SYMBOLS['bullet']} Backup will be created at the remote path")
        click.echo(f"  {SYMBOLS['bullet']} Use 'satdeploy rollback {derived_name}' to restore")
        click.echo(f"  {SYMBOLS['arrow']} {derived_name}")
        click.echo(f"    local:  {local_path} ({size_str})")
        click.echo(f"    remote: {remote_override}")

    else:
        # Handle --all vs app list
        if not apps and not all_apps:
            raise SatDeployError("Specify app names or use --all")
        if apps and all_apps:
            raise SatDeployError("Cannot use both app names and --all")

        if all_apps:
            apps = tuple(config.get_all_app_names())
            if not apps:
                raise SatDeployError("No apps configured")

    module_config = config.get_target()
    if node_override:
        module_config.agent_node = node_override

    # Only allow single app when using --local override
    if local and len(apps) > 1 and not adhoc_mode:
        raise SatDeployError("--local can only be used with a single app")

    # Validate all apps exist and have local files (skip for ad-hoc — already validated)
    if not adhoc_mode:
        for app_name in apps:
            app_cfg = get_app_config_or_error(config, app_name)
            local_path_check = os.path.expanduser(local or app_cfg.local) if len(apps) == 1 else os.path.expanduser(app_cfg.local)
            if not os.path.exists(local_path_check):
                raise SatDeployError(f"Local file not found: {local_path_check}")

    # Helper to resolve app config: ad-hoc map first, then config lookup
    def _get_app_cfg(name: str) -> AppConfig:
        if name in adhoc_app_configs:
            return adhoc_app_configs[name]
        return get_app_config_or_error(config, name)

    # Resolve provenance for each app (fail fast before connecting)
    provenance_map = {}  # app_name -> (provenance_string, source)
    for app_name in apps:
        app_cfg = _get_app_cfg(app_name)
        local_path_prov = os.path.expanduser(local or app_cfg.local) if len(apps) == 1 else os.path.expanduser(app_cfg.local)
        provenance, prov_source = resolve_provenance(local_path_prov)
        provenance_map[app_name] = (provenance, prov_source)

        if prov_source == "local" and is_dirty(provenance):
            click.echo(warning(f"Deploying from uncommitted changes — file tagged as {provenance}"))

    history = get_history(config.history_path)

    # CSP and local transports: use transport abstraction
    if module_config.transport in ("csp", "local"):
        # Local transport needs the apps dict so it knows where to back up to
        apps_dict_for_transport = {
            name: {"remote": cfg.get("remote", ""), "service": cfg.get("service")}
            for name, cfg in config.apps.items()
        } if module_config.transport == "local" and config.apps else None
        transport = get_transport(
            module_config, config.backup_dir, apps=apps_dict_for_transport,
        )
        if module_config.transport == "csp":
            click.echo(f"Connecting to {module_config.zmq_endpoint}...")

        try:
            transport.connect()

            for app in apps:
                app_config = _get_app_cfg(app)
                local_path = os.path.expanduser(local or app_config.local) if len(apps) == 1 else os.path.expanduser(app_config.local)
                remote_path = app_config.remote

                file_size = os.path.getsize(local_path)

                def _show_progress(bytes_sent, total):
                    pct = int(bytes_sent * 100 / total) if total > 0 else 100
                    bar_width = 20
                    filled = int(bar_width * bytes_sent / total) if total > 0 else bar_width
                    bar = "█" * filled + "░" * (bar_width - filled)
                    click.echo(f"\r  Uploading {app}: {bar} {pct}% ({bytes_sent}/{total} bytes)", nl=False)
                    if bytes_sent >= total:
                        click.echo()  # newline when done

                if module_config.transport == "csp":
                    click.echo(f"Deploying {app} via CSP ({file_size} bytes)...")

                deploy_kwargs = dict(
                    app_name=app,
                    local_path=local_path,
                    remote_path=remote_path,
                    force=force,
                )
                if module_config.transport == "csp":
                    deploy_kwargs.update(
                        param_name=app_config.param,
                        appsys_node=module_config.appsys_node,
                        run_node=module_config.get_run_node(app),
                        on_progress=_show_progress,
                    )
                result = transport.deploy(**deploy_kwargs)

                if result.success:
                    # Compute local hash for history
                    local_hash = compute_file_hash(local_path)

                    # Post-deploy health check: only check if app has a service or param
                    if app_config.service or app_config.param:
                        try:
                            app_statuses = transport.get_status()
                            app_status = app_statuses.get(app)
                            if app_status and app_status.running:
                                click.echo(success(f"Health check passed for {app}"))
                            elif app_status:
                                click.echo(warning(f"Health check: {app} is not running"))
                            else:
                                click.echo(warning(f"Health check: {app} not found in agent status"))
                        except TransportError:
                            click.echo(warning(f"Health check: status query timed out"))

                    prov_tuple = provenance_map.get(app, (None, "local"))
                    provenance, prov_source = prov_tuple
                    history.record(DeploymentRecord(
                        module=config.module_name,
                        app=app,
                        file_hash=local_hash,
                        remote_path=remote_path,
                        backup_path=result.backup_path,
                        action="push",
                        success=True,
                        git_hash=provenance,
                        provenance_source=prov_source,
                    ))
                    provenance_display = f" ({provenance})" if provenance else ""
                    click.echo(success(f"Deployed {app} ({local_hash}){provenance_display}"))
                else:
                    history.record(DeploymentRecord(
                        module=config.module_name,
                        app=app,
                        file_hash="",
                        remote_path=remote_path,
                        action="push",
                        success=False,
                        error_message=result.error_message,
                    ))
                    raise SatDeployError(result.error_message or "Deploy failed")

        except TransportError as e:
            # Record failure against the app that was being deployed
            failed_app = app if 'app' in dir() else (apps[0] if apps else "")
            history.record(DeploymentRecord(
                module=config.module_name,
                app=failed_app,
                file_hash="",
                remote_path="",
                action="push",
                success=False,
                error_message=str(e),
            ))
            raise SatDeployError(str(e))
        finally:
            transport.disconnect()

        return

    # SSH transport: use transport abstraction
    apps_dict = {
        name: {"remote": cfg.get("remote", ""), "service": cfg.get("service")}
        for name, cfg in config.apps.items()
    } if config.apps else {}
    # Include ad-hoc apps in transport config
    for name, acfg in adhoc_app_configs.items():
        apps_dict[name] = {"remote": acfg.remote, "service": None}
    transport = get_transport(module_config, config.backup_dir, apps=apps_dict)
    click.echo(f"Connecting to {module_config.host}...")

    try:
        transport.connect()

        for app in apps:
            app_config = _get_app_cfg(app)
            local_path = os.path.expanduser(local or app_config.local) if len(apps) == 1 else os.path.expanduser(app_config.local)
            remote_path = app_config.remote
            service = app_config.service

            services_to_manage = [] if app in adhoc_app_configs else get_services_to_manage(config, app, service)

            click.echo(f"Deploying {app}...")

            result = transport.deploy(
                app_name=app,
                local_path=local_path,
                remote_path=remote_path,
                services=services_to_manage,
                force=force,
            )

            if result.success:
                local_hash = result.file_hash or compute_file_hash(local_path)

                # Sync service file if transport exposes SSH internals
                service_hash = None
                if hasattr(transport, 'ssh') and transport.ssh and hasattr(transport, 'service_manager'):
                    service_hash = sync_service_file(
                        transport.ssh, transport.service_manager,
                        app_config, module_config,
                    )

                prov_tuple = provenance_map.get(app, (None, "local"))
                provenance, prov_source = prov_tuple
                history.record(DeploymentRecord(
                    module=config.module_name,
                    app=app,
                    file_hash=local_hash,
                    remote_path=remote_path,
                    backup_path=result.backup_path,
                    action="push",
                    success=True,
                    service_hash=service_hash,
                    git_hash=provenance,
                    provenance_source=prov_source,
                ))

                provenance_display = f" ({provenance})" if provenance else ""
                if result.skipped:
                    click.echo(warning(f"{app} ({local_hash}) is already deployed. Marked as current."))
                elif result.restored:
                    click.echo(warning(f"{app} ({local_hash}) restored from backup. Marked as current."))
                else:
                    click.echo(success(f"Deployed {app} ({local_hash}){provenance_display}"))

                # Post-deploy health check for services
                if service and hasattr(transport, 'service_manager') and transport.service_manager:
                    svc_status = transport.service_manager.get_status(service)
                    if svc_status == ServiceStatus.RUNNING:
                        click.echo(success(f"Health check passed for {app}"))
                    elif svc_status == ServiceStatus.FAILED:
                        click.echo(warning(f"Health check: {app} service is in failed state"))
                    elif not result.skipped:
                        click.echo(warning(f"Health check: {app} is not running"))
            else:
                history.record(DeploymentRecord(
                    module=config.module_name,
                    app=app,
                    file_hash="",
                    remote_path=remote_path,
                    action="push",
                    success=False,
                    error_message=result.error_message,
                ))
                raise SatDeployError(result.error_message or "Deploy failed")

    except TransportError as e:
        failed_app = app if 'app' in dir() else (apps[0] if apps else "")
        history.record(DeploymentRecord(
            module=config.module_name,
            app=failed_app,
            file_hash="",
            remote_path="",
            action="push",
            success=False,
            error_message=str(e),
        ))
        raise SatDeployError(str(e))
    finally:
        transport.disconnect()




@main.command()
@config_option
@node_option
def status(config_path: Path | None, node_override: int | None):
    """Show status of deployed apps and services."""
    config = load_config(config_path)

    module_config = config.get_target()
    if node_override:
        module_config.agent_node = node_override
    apps = config.apps
    history = get_history(config.history_path)

    # CSP and local transports: use transport abstraction
    if module_config.transport in ("csp", "local"):
        apps_dict_for_transport = {
            name: {"remote": cfg.get("remote", ""), "service": cfg.get("service")}
            for name, cfg in config.apps.items()
        } if module_config.transport == "local" and config.apps else None
        transport = get_transport(
            module_config, config.backup_dir, apps=apps_dict_for_transport,
        )
        if module_config.transport == "csp":
            click.echo(f"Target: node {module_config.agent_node}")
        else:
            click.echo(f"Target: {module_config.target_dir}")
        click.echo("")

        # Collect all app names: configured + any ad-hoc from history
        all_app_names = list(apps.keys()) if apps else []
        module_state = history.get_module_state(config.module_name)
        # Only show ad-hoc apps that had at least one successful deploy
        adhoc_apps = [name for name in module_state
                      if name not in all_app_names and module_state[name].success]

        if not all_app_names and not adhoc_apps:
            click.echo("No apps configured or deployed.")
            return

        # Print header
        header = f"    {'APP':<16}\t{'STATUS':<14}\t{'HASH':<10}\t{'PATH'}"
        click.echo(click.style(header, fg="bright_black"))
        click.echo(click.style("    " + "-" * 60, fg="bright_black"))

        try:
            transport.connect()
            app_statuses = transport.get_status()

            for app_name in all_app_names + adhoc_apps:
                app_status = app_statuses.get(app_name)

                # Provenance map keyed by file_hash — lets status show the
                # original git commit for the file currently on target, even
                # after a rollback (rollback records don't carry git_hash,
                # but the push that originally introduced this hash does).
                app_prov_map = build_provenance_map(history, app_name)

                if app_status:
                    hash_display = app_status.file_hash or "-"
                    remote_path = app_status.remote_path or ""
                    if app_status.running:
                        symbol = click.style(SYMBOLS["check"], fg="green")
                        status_text = "running"
                        status_color = "green"
                    else:
                        symbol = click.style(SYMBOLS["bullet"], fg="green")
                        status_text = "deployed"
                        status_color = "green"

                    git_prov = app_prov_map.get(hash_display)
                else:
                    # Not in agent status — check history for previous deploys
                    last_deploy = module_state.get(app_name)
                    if last_deploy and last_deploy.success:
                        symbol = click.style(SYMBOLS["bullet"], fg="bright_black")
                        status_text = "deployed"
                        status_color = "bright_black"
                        hash_display = last_deploy.file_hash or "-"
                        remote_path = last_deploy.remote_path or ""
                        git_prov = app_prov_map.get(hash_display) or last_deploy.git_hash
                    else:
                        symbol = click.style(SYMBOLS["bullet"], fg="yellow")
                        status_text = "not deployed"
                        status_color = "yellow"
                        hash_display = "-"
                        remote_path = ""
                        git_prov = None

                name_col = f"{app_name:<16}"
                status_col = f"{status_text:<14}"
                hash_text = hash_display
                if git_prov:
                    hash_text += f" ({git_prov})"
                hash_col = f"{hash_text:<10}"

                click.echo(
                    f"  {symbol} {name_col}\t"
                    f"{click.style(status_col, fg=status_color)}\t"
                    f"{click.style(hash_col, fg='white')}\t"
                    f"{click.style(remote_path, fg='bright_black')}"
                )

        except TransportError as e:
            raise SatDeployError(str(e))
        finally:
            transport.disconnect()

        return

    # SSH transport: use direct SSH connection
    target = {"host": module_config.host, "user": module_config.user}
    click.echo(f"Target: {target['host']} ({target['user']})")
    click.echo("")

    # Collect all app names: configured + any ad-hoc from history
    ssh_all_app_names = list(apps.keys()) if apps else []
    ssh_module_state = history.get_module_state(config.module_name)
    ssh_adhoc_apps = [name for name in ssh_module_state
                      if name not in ssh_all_app_names and ssh_module_state[name].success]

    if not ssh_all_app_names and not ssh_adhoc_apps:
        click.echo("No apps configured or deployed.")
        return

    # Print header
    header = f"    {'APP':<16}\t{'STATUS':<14}\t{'HASH':<10}\t{'PATH'}"
    click.echo(click.style(header, fg="bright_black"))
    click.echo(click.style("    " + "-" * 60, fg="bright_black"))

    try:
        with SSHClient(host=target["host"], user=target["user"]) as ssh:
            service_manager = ServiceManager(ssh)

            # Build combined app configs: configured apps + ad-hoc from history
            all_apps_to_show = dict(apps) if apps else {}
            for adhoc_name in ssh_adhoc_apps:
                rec = ssh_module_state[adhoc_name]
                all_apps_to_show[adhoc_name] = {"remote": rec.remote_path, "service": None, "_adhoc": True}

            for app_name, app_config in all_apps_to_show.items():
                service = app_config.get("service")
                remote_path = app_config.get("remote", "")

                # First check if file exists
                deployed = ssh.file_exists(remote_path)

                # Get hash and git provenance from history (only if actually deployed)
                hash_display = "-"
                git_prov = None
                if deployed:
                    last_deploy = history.get_last_deployment(app_name)
                    if last_deploy and last_deploy.success:
                        hash_display = last_deploy.file_hash or "-"
                        git_prov = last_deploy.git_hash

                if not deployed:
                    symbol = click.style(SYMBOLS["bullet"], fg="yellow")
                    status_text = "not deployed"
                    status_color = "yellow"
                elif service:
                    # File exists and has a service - check service status
                    svc_status = service_manager.get_status(service)
                    if svc_status == ServiceStatus.RUNNING:
                        symbol = click.style(SYMBOLS["check"], fg="green")
                        status_text = "running"
                        status_color = "green"
                    elif svc_status == ServiceStatus.STOPPED:
                        symbol = click.style(SYMBOLS["bullet"], fg="yellow")
                        status_text = "stopped"
                        status_color = "yellow"
                    elif svc_status == ServiceStatus.FAILED:
                        symbol = click.style(SYMBOLS["cross"], fg="red")
                        status_text = "failed"
                        status_color = "red"
                    else:
                        symbol = click.style(SYMBOLS["bullet"], fg="white")
                        status_text = "unknown"
                        status_color = "white"
                else:
                    # File exists but no service (library)
                    symbol = click.style(SYMBOLS["bullet"], fg="green")
                    status_text = "deployed"
                    status_color = "green"

                name_col = f"{app_name:<16}"
                status_col = f"{status_text:<14}"
                hash_text = hash_display
                if git_prov:
                    hash_text += f" ({git_prov})"
                hash_col = f"{hash_text:<10}"

                click.echo(
                    f"  {symbol} {name_col}\t"
                    f"{click.style(status_col, fg=status_color)}\t"
                    f"{click.style(hash_col, fg='white')}\t"
                    f"{click.style(remote_path, fg='bright_black')}"
                )

    except SSHError as e:
        raise SatDeployError(str(e))


@main.command("list")
@click.argument("app", type=APP_NAME)
@config_option
@node_option
def list_backups(app: str, config_path: Path | None, node_override: int | None):
    """List all versions of an app (deployed + backups).

    APP is the name of the application to list versions for.

    Shows the currently deployed version at the top, followed by
    all available backups that can be restored via rollback.
    """
    config = load_config(config_path)

    module_config = config.get_target()
    if node_override:
        module_config.agent_node = node_override
    history = get_history(config.history_path)

    # Get currently deployed version from history
    last_deploy = history.get_last_deployment(app)

    # CSP and local transports: use transport abstraction
    if module_config.transport in ("csp", "local"):
        apps_dict_for_transport = {
            name: {"remote": cfg.get("remote", ""), "service": cfg.get("service")}
            for name, cfg in config.apps.items()
        } if module_config.transport == "local" and config.apps else None
        transport = get_transport(
            module_config, config.backup_dir, apps=apps_dict_for_transport,
        )

        try:
            transport.connect()
            backup_infos = transport.list_backups(app)

            # Get currently deployed hash
            current_hash = None
            if last_deploy and last_deploy.success:
                current_hash = last_deploy.file_hash

            # Convert BackupInfo to dict format and deduplicate
            seen_keys = {}
            for backup in backup_infos:
                key = backup.file_hash or backup.version
                if key and key not in seen_keys:
                    seen_keys[key] = {
                        "hash": backup.file_hash,
                        "timestamp": backup.timestamp,
                        "path": backup.path,
                    }

            # Add currently deployed version if not in backups
            if current_hash and current_hash not in seen_keys:
                timestamp_display = format_iso_timestamp(last_deploy.timestamp)
                seen_keys[current_hash] = {
                    "hash": current_hash,
                    "timestamp": timestamp_display,
                }

            # Build unified list sorted by timestamp (newest first)
            versions = list(seen_keys.values())
            versions.sort(key=lambda v: v.get("timestamp", ""), reverse=True)

            if not versions:
                click.echo(f"No versions found for {app}.")
                return

            click.echo(click.style(f"Versions for {app}:", bold=True))
            click.echo("")
            # Print header
            header = f"    {'HASH':<10}\t{'TIMESTAMP':<20}\t{'STATUS'}"
            click.echo(click.style(header, fg="bright_black"))
            click.echo(click.style("    " + "-" * 45, fg="bright_black"))

            git_hash_map = build_provenance_map(history, app)

            # Show all versions, arrow on deployed one
            for version in versions:
                hash_display = version.get("hash") or "-"
                timestamp_display = version.get("timestamp") or "-"
                is_deployed = hash_display == current_hash
                git_prov = git_hash_map.get(hash_display)
                hash_text = hash_display
                if git_prov:
                    hash_text += f" ({git_prov})"

                if is_deployed:
                    bullet = click.style(SYMBOLS["arrow"], fg="green")
                    hash_col = click.style(f"{hash_text:<10}", fg="green")
                    status_col = click.style("deployed", fg="green")
                else:
                    bullet = click.style(SYMBOLS["bullet"], fg="blue")
                    hash_col = click.style(f"{hash_text:<10}", fg="blue")
                    status_col = click.style("backup", fg="blue")

                timestamp_col = click.style(f"{timestamp_display:<20}", fg="bright_black")
                click.echo(f"  {bullet} {hash_col}\t{timestamp_col}\t{status_col}")

        except TransportError as e:
            raise SatDeployError(str(e))
        finally:
            transport.disconnect()

        return

    # SSH transport: needs app config for remote path lookup
    app_config = get_app_config_or_error(config, app)
    target = {"host": module_config.host, "user": module_config.user}
    with SSHClient(host=target["host"], user=target["user"]) as ssh:
        deployer = Deployer(
            ssh=ssh,
            backup_dir=config.backup_dir,
            max_backups=config.max_backups,
        )

        try:
            backups = deployer.list_backups(app)

            # Get currently deployed hash
            current_hash = None
            if last_deploy and last_deploy.success:
                current_hash = last_deploy.file_hash

            # Deduplicate backups by hash, keeping most recent (first in list)
            seen_keys = {}
            for backup in backups:
                # Use hash if available, otherwise use version string
                key = backup.get("hash") or backup.get("version")
                if key and key not in seen_keys:
                    seen_keys[key] = backup

            # Add currently deployed version if not in backups (e.g., after first push)
            # Use history timestamp only for versions with no backup yet
            if current_hash and current_hash not in seen_keys:
                timestamp_display = format_iso_timestamp(last_deploy.timestamp)
                seen_keys[current_hash] = {
                    "hash": current_hash,
                    "timestamp": timestamp_display,
                }

            # Build unified list sorted by timestamp (newest first)
            versions = list(seen_keys.values())
            versions.sort(key=lambda v: v.get("timestamp", ""), reverse=True)

            if not versions:
                click.echo(f"No versions found for {app}.")
                return

            click.echo(click.style(f"Versions for {app}:", bold=True))
            click.echo("")
            # Print header
            header = f"    {'HASH':<10}\t{'TIMESTAMP':<20}\t{'STATUS'}"
            click.echo(click.style(header, fg="bright_black"))
            click.echo(click.style("    " + "-" * 45, fg="bright_black"))

            git_hash_map = build_provenance_map(history, app)

            # Show all versions, arrow on deployed one
            for version in versions:
                hash_display = version.get("hash") or "-"
                timestamp_display = version.get("timestamp") or "-"
                is_deployed = hash_display == current_hash
                git_prov = git_hash_map.get(hash_display)
                hash_text = hash_display
                if git_prov:
                    hash_text += f" ({git_prov})"

                if is_deployed:
                    bullet = click.style(SYMBOLS["arrow"], fg="green")
                    hash_col = click.style(f"{hash_text:<10}", fg="green")
                    status_col = click.style("deployed", fg="green")
                else:
                    bullet = click.style(SYMBOLS["bullet"], fg="blue")
                    hash_col = click.style(f"{hash_text:<10}", fg="blue")
                    status_col = click.style("backup", fg="blue")

                timestamp_col = click.style(f"{timestamp_display:<20}", fg="bright_black")
                click.echo(f"  {bullet} {hash_col}\t{timestamp_col}\t{status_col}")

        except SSHError as e:
            raise SatDeployError(str(e))


@main.command()
@click.argument("app", type=APP_NAME)
@click.argument("hash", required=False, default=None)
@click.option("-H", "--hash", "hash_option", default=None,
              help="Specific backup hash to restore")
@config_option
@node_option
def rollback(app: str, hash: str | None, hash_option: str | None, config_path: Path | None, node_override: int | None):  # noqa: A002
    """Rollback to a previous version.

    APP is the name of the application to rollback.
    HASH is the optional backup hash to restore (defaults to previous version).
    """
    target_hash = hash_option or hash  # -H flag takes precedence over positional
    config = load_config(config_path)

    module_config = config.get_target()
    if node_override:
        module_config.agent_node = node_override
    history = get_history(config.history_path)
    backup_path = None

    # CSP and local transports: use transport abstraction
    if module_config.transport in ("csp", "local"):
        apps_dict_for_transport = {
            name: {"remote": cfg.get("remote", ""), "service": cfg.get("service")}
            for name, cfg in config.apps.items()
        } if module_config.transport == "local" and config.apps else None
        transport = get_transport(
            module_config, config.backup_dir, apps=apps_dict_for_transport,
        )
        if module_config.transport == "csp":
            click.echo(f"Connecting to {module_config.zmq_endpoint}...")

        try:
            transport.connect()

            if module_config.transport == "csp":
                click.echo(f"Rolling back {app} via CSP...")
            else:
                click.echo(f"Rolling back {app}...")
            result = transport.rollback(
                app_name=app,
                backup_hash=target_hash,
            )

            if result.success:
                # Use the actual backup hash from the response if available
                actual_hash = target_hash or (result.backup_path or "").split("-")[-1].replace(".bak", "") or ""
                history.record(DeploymentRecord(
                    module=config.module_name,
                    app=app,
                    file_hash=actual_hash,
                    remote_path="",
                    action="rollback",
                    success=True,
                ))
                if actual_hash:
                    click.echo(success(f"Rolled back {app} to {actual_hash}"))
                else:
                    click.echo(success(f"Rolled back {app}"))
            else:
                history.record(DeploymentRecord(
                    module=config.module_name,
                    app=app,
                    file_hash="",
                    remote_path="",
                    action="rollback",
                    success=False,
                    error_message=result.error_message,
                ))
                raise SatDeployError(result.error_message or "Rollback failed")

        except TransportError as e:
            history.record(DeploymentRecord(
                module=config.module_name,
                app=app,
                file_hash="",
                remote_path="",
                action="rollback",
                success=False,
                error_message=str(e),
            ))
            raise SatDeployError(str(e))
        finally:
            transport.disconnect()

        return

    # SSH transport: needs app config for remote path and service management
    app_config = get_app_config_or_error(config, app)
    remote_path = app_config.remote
    service = app_config.service
    target = {"host": module_config.host, "user": module_config.user}
    click.echo(f"Connecting to {target['host']}...")

    try:
        with SSHClient(host=target["host"], user=target["user"]) as ssh:
            service_manager = ServiceManager(ssh)
            deployer = Deployer(
                ssh=ssh,
                backup_dir=config.backup_dir,
                max_backups=config.max_backups,
            )

            services_to_manage = get_services_to_manage(config, app, service)

            # Get backup list and find the right one
            raw_backups = deployer.list_backups(app)
            if not raw_backups:
                raise SatDeployError("No backups available for rollback")

            # Deduplicate backups by hash, keeping most recent (first in list)
            # This prevents the dial from bouncing between duplicate backups
            # Skip old-format backups without hash - they're not supported
            seen_hashes = set()
            backups = []
            for b in raw_backups:
                h = b.get("hash")
                if h and h not in seen_hashes:
                    seen_hashes.add(h)
                    backups.append(b)

            if not backups:
                raise SatDeployError("No backups available for rollback")

            # Get currently deployed hash to find position in version history
            last_deploy = history.get_last_deployment(app)
            current_hash = last_deploy.file_hash if last_deploy and last_deploy.success else None

            if target_hash:
                # Match by hash prefix
                matching = [b for b in raw_backups if b.get("hash") == target_hash]
                if not matching:
                    raise SatDeployError(f"Hash {target_hash} not found")
                backup = matching[0]
            elif current_hash:
                # Dial behavior: find current position and go to next older version
                # Backups are deduplicated and sorted newest-first
                current_index = None
                for i, b in enumerate(backups):
                    if b.get("hash") == current_hash:
                        current_index = i
                        break

                if current_index is not None:
                    # Current version is in backups, go to next older
                    next_index = current_index + 1
                    if next_index >= len(backups):
                        click.echo(warning("Already at oldest version. No older backup available."))
                        return
                    backup = backups[next_index]
                else:
                    # Current version not in backups (fresh deploy), go to most recent
                    backup = backups[0]
            else:
                # No history, just use the most recent backup
                backup = backups[0]

            backup_path = backup["path"]
            backup_hash = backup.get("hash") or "-"
            backup_timestamp = backup.get("timestamp") or "-"

            # Check if current version needs to be backed up (not already in backups)
            backup_hashes = {b.get("hash") for b in backups if b.get("hash")}
            needs_backup = current_hash and current_hash not in backup_hashes

            total_steps = (1 if needs_backup else 0) + 1 + len(services_to_manage) * 2
            counter = StepCounter(total_steps)

            click.echo(f"Rolling back {app}...")

            stop_services(service_manager, services_to_manage, counter)

            if needs_backup:
                remote_target = f"{target['user']}@{target['host']}:{remote_path}"
                counter.next(f"Backing up {remote_target}")
                deployer.backup(app, remote_path)

            counter.next(f"Restoring {backup_hash} ({backup_timestamp})")
            deployer.restore(backup_path, remote_path)

            start_services(service_manager, services_to_manage, counter)

            # Log successful rollback
            history.record(DeploymentRecord(
                module=config.module_name,
                app=app,
                file_hash=backup_hash,
                remote_path=remote_path,
                backup_path=backup_path,
                action="rollback",
                success=True,
            ))

            click.echo(success(f"Rolled back {app} to {backup_hash} ({backup_timestamp})"))

    except SSHError as e:
        # Log failed rollback
        history.record(DeploymentRecord(
            module=config.module_name,
            app=app,
            file_hash="",
            remote_path=remote_path,
            backup_path=backup_path or "",
            action="rollback",
            success=False,
            error_message=str(e),
        ))
        raise SatDeployError(str(e))


@main.command()
@config_option
def config(config_path: Path | None):
    """Show current configuration."""
    cfg = load_config(config_path)

    click.echo(f"Config file: {cfg.config_path}")

    module = cfg.get_target()

    # Defaults block (matches APM format)
    click.echo(f"\nDefaults:")
    if module.appsys_node:
        click.echo(f"  appsys_node: {module.appsys_node}")
    else:
        click.echo(f"  appsys_node: 0 (restart disabled)")

    # Transport block (Python CLI-specific, APM doesn't need this)
    click.echo(f"\nTransport:")
    click.echo(f"  type:          {module.transport}")
    if module.transport == "ssh":
        click.echo(f"  host:          {module.host}")
        click.echo(f"  user:          {module.user}")
    elif module.transport == "csp":
        click.echo(f"  zmq_endpoint:  {module.zmq_endpoint}")
        click.echo(f"  agent_node:    {module.agent_node}")
        click.echo(f"  ground_node:   {module.ground_node}")
    click.echo(f"  backup_dir:    {cfg.backup_dir}")

    apps = cfg.apps
    click.echo(f"\nApps: {len(apps) if apps else 0}")
    if not apps:
        click.echo("  (none configured)")
        return

    for app_name, app_data in apps.items():
        click.echo(f"  {app_name}:")
        click.echo(f"    local:       {app_data.get('local', '-')}")
        click.echo(f"    remote:      {app_data.get('remote', '-')}")
        if app_data.get("service"):
            click.echo(f"    service:     {app_data['service']}")
        if app_data.get("param"):
            click.echo(f"    param:       {app_data['param']}")


@main.command()
@click.argument("app", type=APP_NAME)
@click.option(
    "-l", "--lines",
    type=int,
    default=100,
    help="Number of lines to show (default: 100)",
)
@config_option
@node_option
def logs(app: str, lines: int, config_path: Path | None, node_override: int | None):
    """Show logs for an app's service.

    APP is the name of the application to show logs for.
    """
    config = load_config(config_path)

    module_config = config.get_target()
    if node_override:
        module_config.agent_node = node_override

    app_config = get_app_config_or_error(config, app)

    service = app_config.service
    if not service:
        raise SatDeployError(
            f"App '{app}' is a library and has no service. Cannot show logs."
        )

    module_config = config.get_target()

    transport = get_transport(module_config, config.backup_dir)
    try:
        transport.connect()
        click.echo(click.style(f"Logs for {app} ({service}):", bold=True))
        click.echo("")
        log_output = transport.get_logs(app, service, lines=lines)
        if log_output:
            click.echo(log_output)
        else:
            raise SatDeployError(f"Could not retrieve logs for {app}")
    finally:
        transport.disconnect()


@main.group(cls=ColoredGroup, invoke_without_command=True)
@click.pass_context
def demo(ctx: click.Context):
    """Set up a zero-prerequisite demo environment.

    Running `satdeploy demo` with no subcommand is equivalent to
    `satdeploy demo start` — it's the fastest path to trying satdeploy.
    """
    if ctx.invoked_subcommand is None:
        demo_module.demo_start()


@demo.command()
def start():
    """Set up the demo environment (throwaway git repo + local target)."""
    demo_module.demo_start()


@demo.command()
@click.option("--clean", is_flag=True, help="Remove all demo files")
def stop(clean: bool):
    """Tear down the demo environment."""
    demo_module.demo_stop(clean=clean)


@demo.command("status")
def demo_status_cmd():
    """Show demo environment status."""
    demo_module.demo_status()


