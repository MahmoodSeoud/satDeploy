"""CLI entry point for satdeploy."""

import os
from datetime import datetime
from pathlib import Path

import click

from satdeploy.config import DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_FILE, Config, ModuleConfig, AppConfig
from satdeploy.dependencies import DependencyResolver
from satdeploy.deployer import Deployer
from satdeploy.hash import compute_file_hash
from satdeploy.provenance import capture_provenance, is_dirty
from satdeploy.history import DeploymentRecord, History
from satdeploy.output import success, warning, step, SYMBOLS, SatDeployError, ColoredGroup
from satdeploy.services import ServiceManager, ServiceStatus
from satdeploy.ssh import SSHClient, SSHError
from satdeploy.templates import render_service_template, compute_service_hash
from satdeploy.transport import Transport, SSHTransport, CSPTransport, TransportError
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
        )
    else:
        raise ValueError(f"Unknown transport type: {module.transport}")


def get_history(db_path: Path) -> History:
    """Get or create the history database."""
    history = History(db_path)
    history.init_db()
    return history


def build_provenance_map(history: History, app: str) -> dict[str, str]:
    """Build a mapping from binary hash to git provenance string.

    Args:
        history: The history database.
        app: The app name to look up.

    Returns:
        Dict mapping binary_hash to git_hash provenance string.
    """
    prov_map = {}
    for rec in history.get_history(app):
        if rec.binary_hash and rec.git_hash and rec.binary_hash not in prov_map:
            prov_map[rec.binary_hash] = rec.git_hash
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


@click.group(cls=ColoredGroup)
def main():
    """Deploy binaries to embedded Linux targets."""
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
    data["apps"] = {}

    config.save(data)
    click.echo("")
    click.echo(success(f"Config saved to {config.config_path}"))


@main.command()
@click.argument("apps", nargs=-1)
@click.option("--all", "all_apps", is_flag=True, help="Deploy all apps")
@click.option("--clean-vmem", is_flag=True, help="Clear vmem for deployed apps")
@click.option(
    "--local",
    type=click.Path(exists=False),
    default=None,
    help="Override local path for the binary",
)
@config_option
@click.option("--dry-run", is_flag=True, help="Show what would happen without deploying")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation prompts")
@click.option("--require-clean", is_flag=True, help="Refuse to deploy from dirty git tree")
def push(
    apps: tuple[str, ...],
    all_apps: bool,
    clean_vmem: bool,
    local: str | None,
    config_path: Path | None,
    dry_run: bool,
    yes: bool,
    require_clean: bool,
):
    """Deploy one or more apps to a target.

    APPS are the names of the applications to deploy.
    """
    config = Config(config_path=config_path)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

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

    # Only allow single app when using --local override
    if local and len(apps) > 1:
        raise SatDeployError("--local can only be used with a single app")

    # Validate all apps exist and have local files
    for app_name in apps:
        app_cfg = get_app_config_or_error(config, app_name)
        local_path_check = os.path.expanduser(local or app_cfg.local) if len(apps) == 1 else os.path.expanduser(app_cfg.local)
        if not os.path.exists(local_path_check):
            raise SatDeployError(f"Local file not found: {local_path_check}")

    # Dry-run: show what would happen and exit
    if dry_run:
        target_name = module_config.zmq_endpoint if module_config.transport == "csp" else module_config.host
        click.echo(click.style(f"Dry run — no changes will be made", bold=True))
        click.echo(f"Target: {target_name} ({module_config.transport})")
        click.echo("")
        for app_name in apps:
            app_cfg = get_app_config_or_error(config, app_name)
            lp = os.path.expanduser(local or app_cfg.local) if len(apps) == 1 else os.path.expanduser(app_cfg.local)
            file_size = os.path.getsize(lp)
            local_hash = compute_file_hash(lp)
            size_str = f"{file_size / 1024:.1f} KB" if file_size < 1024 * 1024 else f"{file_size / (1024 * 1024):.1f} MB"
            click.echo(f"  {SYMBOLS['arrow']} {app_name}")
            click.echo(f"    local:   {lp} ({size_str}, {local_hash})")
            click.echo(f"    remote:  {app_cfg.remote}")
            if app_cfg.service:
                services = get_services_to_manage(config, app_name, app_cfg.service)
                svc_names = [s[1] for s in services]
                click.echo(f"    services: {', '.join(svc_names)} (will restart)")
        click.echo("")
        click.echo("Run without --dry-run to deploy.")
        return

    # Capture git provenance for each app (fail fast before connecting)
    provenance_map = {}
    for app_name in apps:
        app_cfg = get_app_config_or_error(config, app_name)
        local_path_prov = os.path.expanduser(local or app_cfg.local) if len(apps) == 1 else os.path.expanduser(app_cfg.local)
        provenance = capture_provenance(local_path_prov)
        provenance_map[app_name] = provenance

        if require_clean and is_dirty(provenance):
            raise SatDeployError(
                "Refusing to deploy from dirty git tree. Commit your changes first."
            )

        if is_dirty(provenance):
            click.echo(warning(f"Deploying from uncommitted changes — binary tagged as {provenance}"))

    # Confirmation prompt for multi-app deploys
    if len(apps) > 1 and not yes:
        target_name = module_config.zmq_endpoint if module_config.transport == "csp" else module_config.host
        click.echo(f"This will deploy {len(apps)} apps to {config.module_name} ({target_name}):")
        for app_name in apps:
            click.echo(f"  {SYMBOLS['bullet']} {app_name}")
        if not click.confirm("Continue?"):
            click.echo("Aborted.")
            return

    history = get_history(config.history_path)

    # CSP transport: use transport abstraction
    if module_config.transport == "csp":
        transport = get_transport(module_config, config.backup_dir)
        click.echo(f"Connecting to {module_config.zmq_endpoint}...")

        try:
            transport.connect()

            for app in apps:
                app_config = get_app_config_or_error(config, app)
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

                click.echo(f"Deploying {app} via CSP ({file_size} bytes)...")

                result = transport.deploy(
                    app_name=app,
                    local_path=local_path,
                    remote_path=remote_path,
                    param_name=app_config.param,
                    appsys_node=module_config.appsys_node,
                    run_node=module_config.get_run_node(app),
                    on_progress=_show_progress,
                )

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

                    provenance = provenance_map.get(app)
                    history.record(DeploymentRecord(
                        module=config.module_name,
                        app=app,
                        binary_hash=local_hash,
                        remote_path=remote_path,
                        backup_path=result.backup_path,
                        action="push",
                        success=True,
                        git_hash=provenance,
                    ))
                    provenance_display = f" ({provenance})" if provenance else ""
                    click.echo(success(f"Deployed {app} ({local_hash}){provenance_display}"))
                else:
                    history.record(DeploymentRecord(
                        module=config.module_name,
                        app=app,
                        binary_hash="",
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
                binary_hash="",
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
    transport = get_transport(module_config, config.backup_dir, apps=apps_dict)
    click.echo(f"Connecting to {module_config.host}...")

    try:
        transport.connect()

        for app in apps:
            app_config = get_app_config_or_error(config, app)
            local_path = os.path.expanduser(local or app_config.local) if len(apps) == 1 else os.path.expanduser(app_config.local)
            remote_path = app_config.remote
            service = app_config.service

            services_to_manage = get_services_to_manage(config, app, service)

            click.echo(f"Deploying {app}...")

            result = transport.deploy(
                app_name=app,
                local_path=local_path,
                remote_path=remote_path,
                services=services_to_manage,
            )

            if result.success:
                local_hash = result.binary_hash or compute_file_hash(local_path)

                # Sync service file if transport exposes SSH internals
                service_hash = None
                if hasattr(transport, 'ssh') and transport.ssh and hasattr(transport, 'service_manager'):
                    service_hash = sync_service_file(
                        transport.ssh, transport.service_manager,
                        app_config, module_config,
                    )

                provenance = provenance_map.get(app)
                history.record(DeploymentRecord(
                    module=config.module_name,
                    app=app,
                    binary_hash=local_hash,
                    remote_path=remote_path,
                    backup_path=result.backup_path,
                    action="push",
                    success=True,
                    service_hash=service_hash,
                    git_hash=provenance,
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
                    binary_hash="",
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
            binary_hash="",
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
def status(config_path: Path | None):
    """Show status of deployed apps and services."""
    config = Config(config_path=config_path)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    module_config = config.get_target()
    apps = config.apps
    history = get_history(config.history_path)

    # CSP transport: use transport abstraction
    if module_config.transport == "csp":
        transport = get_transport(module_config, config.backup_dir)
        click.echo(f"Target: node {module_config.agent_node}")
        click.echo("")

        if not apps:
            click.echo("No apps configured.")
            return

        # Print header
        header = f"    {'APP':<16}\t{'STATUS':<14}\t{'HASH':<10}\t{'TIMESTAMP'}"
        click.echo(click.style(header, fg="bright_black"))
        click.echo(click.style("    " + "-" * 60, fg="bright_black"))

        try:
            transport.connect()
            app_statuses = transport.get_status()

            for app_name, app_config_dict in apps.items():
                app_status = app_statuses.get(app_name)

                if app_status:
                    hash_display = app_status.binary_hash or "-"
                    if app_status.running:
                        symbol = click.style(SYMBOLS["check"], fg="green")
                        status_text = "running"
                        status_color = "green"
                    else:
                        symbol = click.style(SYMBOLS["bullet"], fg="yellow")
                        status_text = "stopped"
                        status_color = "yellow"

                    # Get timestamp and git provenance from history
                    last_deploy = history.get_last_deployment(app_name)
                    timestamp_display = format_iso_timestamp(last_deploy.timestamp) if last_deploy and last_deploy.success else "-"
                    git_prov = last_deploy.git_hash if last_deploy and last_deploy.success else None
                else:
                    symbol = click.style(SYMBOLS["bullet"], fg="yellow")
                    status_text = "not deployed"
                    status_color = "yellow"
                    hash_display = "-"
                    timestamp_display = "-"
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
                    f"{click.style(timestamp_display, fg='bright_black')}"
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

    if not apps:
        click.echo("No apps configured.")
        return

    # Print header
    header = f"    {'APP':<16}\t{'STATUS':<14}\t{'HASH':<10}\t{'TIMESTAMP'}"
    click.echo(click.style(header, fg="bright_black"))
    click.echo(click.style("    " + "-" * 60, fg="bright_black"))

    try:
        with SSHClient(host=target["host"], user=target["user"]) as ssh:
            service_manager = ServiceManager(ssh)

            for app_name, app_config in apps.items():
                service = app_config.get("service")
                remote_path = app_config.get("remote")

                # First check if file exists
                deployed = ssh.file_exists(remote_path)

                # Get hash, timestamp, and git provenance from history (only if actually deployed)
                hash_display = "-"
                timestamp_display = "-"
                git_prov = None
                if deployed:
                    last_deploy = history.get_last_deployment(app_name)
                    if last_deploy and last_deploy.success:
                        hash_display = last_deploy.binary_hash or "-"
                        timestamp_display = format_iso_timestamp(last_deploy.timestamp)
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

                # Pad plain text first, then colorize
                name_col = f"{app_name:<16}"
                status_col = f"{status_text:<14}"
                hash_text = hash_display
                if git_prov:
                    hash_text += f" ({git_prov})"
                hash_col = f"{hash_text:<10}"
                timestamp_col = timestamp_display

                click.echo(
                    f"  {symbol} {name_col}\t"
                    f"{click.style(status_col, fg=status_color)}\t"
                    f"{click.style(hash_col, fg='white')}\t"
                    f"{click.style(timestamp_col, fg='bright_black')}"
                )

    except SSHError as e:
        raise SatDeployError(str(e))


@main.command("list")
@click.argument("app")
@config_option
def list_backups(app: str, config_path: Path | None):
    """List all versions of an app (deployed + backups).

    APP is the name of the application to list versions for.

    Shows the currently deployed version at the top, followed by
    all available backups that can be restored via rollback.
    """
    config = Config(config_path=config_path)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    module_config = config.get_target()
    history = get_history(config.history_path)

    # Get currently deployed version from history
    last_deploy = history.get_last_deployment(app)

    # CSP transport: use transport abstraction
    # No app config needed — the agent discovers apps dynamically
    if module_config.transport == "csp":
        transport = get_transport(module_config, config.backup_dir)

        try:
            transport.connect()
            backup_infos = transport.list_backups(app)

            # Get currently deployed hash
            current_hash = None
            if last_deploy and last_deploy.success:
                current_hash = last_deploy.binary_hash

            # Convert BackupInfo to dict format and deduplicate
            seen_keys = {}
            for backup in backup_infos:
                key = backup.binary_hash or backup.version
                if key and key not in seen_keys:
                    seen_keys[key] = {
                        "hash": backup.binary_hash,
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
                current_hash = last_deploy.binary_hash

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
@click.argument("app")
@click.argument("hash", required=False, default=None)
@config_option
def rollback(app: str, hash: str | None, config_path: Path | None):  # noqa: A002
    """Rollback to a previous version.

    APP is the name of the application to rollback.
    HASH is the optional backup hash to restore (defaults to previous version).
    """
    target_hash = hash  # Rename to avoid shadowing builtin
    config = Config(config_path=config_path)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    module_config = config.get_target()
    history = get_history(config.history_path)
    backup_path = None

    # CSP transport: use transport abstraction
    # No app config needed — the agent handles rollback internally
    if module_config.transport == "csp":
        transport = get_transport(module_config, config.backup_dir)
        click.echo(f"Connecting to {module_config.zmq_endpoint}...")

        try:
            transport.connect()

            click.echo(f"Rolling back {app} via CSP...")
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
                    binary_hash=actual_hash,
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
                    binary_hash="",
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
                binary_hash="",
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
            current_hash = last_deploy.binary_hash if last_deploy and last_deploy.success else None

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
                binary_hash=backup_hash,
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
            binary_hash="",
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
    cfg = Config(config_path=config_path)

    if cfg.load() is None:
        raise SatDeployError(
            f"Config not found at {cfg.config_path}. Run 'satdeploy init' first."
        )

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
@click.argument("app")
@click.option(
    "--lines",
    "-n",
    type=int,
    default=100,
    help="Number of lines to show (default: 100)",
)
@config_option
def logs(app: str, lines: int, config_path: Path | None):
    """Show logs for an app's service.

    APP is the name of the application to show logs for.
    """
    config = Config(config_path=config_path)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

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


@main.group(cls=ColoredGroup)
def demo():
    """Manage the demo environment (simulated satellite)."""
    pass


@demo.command()
def start():
    """Start a simulated satellite for trying satdeploy."""
    demo_module.demo_start()


@demo.command()
@click.option("--clean", is_flag=True, help="Remove demo config directory")
def stop(clean: bool):
    """Stop the demo environment."""
    demo_module.demo_stop(clean=clean)


@demo.command("status")
def demo_status_cmd():
    """Show demo environment status."""
    demo_module.demo_status()


@demo.command()
def shell():
    """Open an interactive shell on the simulated satellite."""
    demo_module.demo_shell()


@demo.command()
def eject():
    """Generate a real config template for your hardware."""
    demo_module.demo_eject()
