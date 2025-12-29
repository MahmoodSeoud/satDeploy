"""CLI entry point for satdeploy."""

import os
from datetime import datetime
from pathlib import Path

import click

from satdeploy.config import DEFAULT_CONFIG_DIR, Config, ModuleConfig, AppConfig
from satdeploy.dependencies import DependencyResolver
from satdeploy.deployer import Deployer
from satdeploy.history import DeploymentRecord, History
from satdeploy.output import success, warning, step, SYMBOLS, SatDeployError, ColoredGroup
from satdeploy.services import ServiceManager, ServiceStatus
from satdeploy.ssh import SSHClient, SSHError
from satdeploy.templates import render_service_template, compute_service_hash


def get_history(config_dir: Path) -> History:
    """Get or create the history database."""
    history = History(config_dir / "history.db")
    history.init_db()
    return history


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


@click.group(cls=ColoredGroup)
def main():
    """Deploy binaries to embedded Linux targets."""
    pass


@main.command()
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def init(config_dir: Path | None):
    """Interactive setup, creates config.yaml."""
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.config_path.exists():
        if not click.confirm("Config file already exists. Overwrite?"):
            click.echo("Aborted.")
            return

    click.echo(click.style("Setting up satdeploy configuration...", bold=True))
    click.echo("")

    modules = {}
    while True:
        module_name = click.prompt("Module name", default="default" if not modules else None)
        host = click.prompt(f"  {module_name} host (IP or hostname)")
        user = click.prompt(f"  {module_name} user", default="root")
        modules[module_name] = {"host": host, "user": user}
        click.echo("")
        if not click.confirm("Add another module?", default=False):
            break

    data = {
        "modules": modules,
        "backup_dir": "/opt/satdeploy/backups",
        "max_backups": 10,
        "apps": {
            "example_app": {
                "local": "/path/to/local/binary",
                "remote": "/path/to/remote/binary",
                "service": None,
            },
        },
    }

    config.save(data)
    click.echo("")
    click.echo(success(f"Config saved to {config.config_path}"))


@main.command()
@click.argument("apps", nargs=-1)
@click.option("--all", "all_apps", is_flag=True, help="Deploy all apps")
@click.option("--module", "-m", required=True, help="Target module")
@click.option("--clean-vmem", is_flag=True, help="Clear vmem for deployed apps")
@click.option(
    "--local",
    type=click.Path(exists=False),
    default=None,
    help="Override local path for the binary",
)
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def push(
    apps: tuple[str, ...],
    all_apps: bool,
    module: str,
    clean_vmem: bool,
    local: str | None,
    config_dir: Path | None,
):
    """Deploy one or more apps to a module.

    APPS are the names of the applications to deploy.
    """
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

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

    # Get target module
    try:
        module_config = config.get_module(module)
        target = {"host": module_config.host, "user": module_config.user}
    except KeyError:
        raise SatDeployError(f"Module '{module}' not found in config")

    # Only allow single app when using --local override
    if local and len(apps) > 1:
        raise SatDeployError("--local can only be used with a single app")

    # Validate all apps exist and have local files
    for app_name in apps:
        app_cfg = get_app_config_or_error(config, app_name)
        local_path_check = os.path.expanduser(local or app_cfg.local) if len(apps) == 1 else os.path.expanduser(app_cfg.local)
        if not os.path.exists(local_path_check):
            raise SatDeployError(f"Local file not found: {local_path_check}")

    history = get_history(config_dir)

    click.echo(f"Connecting to {target['host']}...")

    try:
        with SSHClient(host=target["host"], user=target["user"]) as ssh:
            service_manager = ServiceManager(ssh)
            deployer = Deployer(
                ssh=ssh,
                backup_dir=config.backup_dir,
                max_backups=config.max_backups,
            )

            # Deploy each app
            for app in apps:
                app_config = get_app_config_or_error(config, app)
                local_path = os.path.expanduser(local or app_config.local) if len(apps) == 1 else os.path.expanduser(app_config.local)
                remote_path = app_config.remote
                service = app_config.service

                services_to_manage = get_services_to_manage(config, app, service)

                # Check if local and remote are the same - skip if already deployed
                local_hash = deployer.compute_hash(local_path)
                remote_hash = deployer.compute_remote_hash(remote_path)

                if local_hash and remote_hash and local_hash == remote_hash:
                    # Binary already deployed, but check if service file needs updating
                    service_hash = sync_service_file(
                        ssh, service_manager, app_config, module_config
                    )
                    # Still record in history so it becomes the "current" version
                    history.record(DeploymentRecord(
                        module=module,
                        app=app,
                        binary_hash=local_hash,
                        remote_path=remote_path,
                        action="push",
                        success=True,
                        service_hash=service_hash,
                    ))
                    click.echo(warning(f"{app} ({local_hash}) is already deployed. Marked as current."))
                    continue

                # Check if local version already exists in backups - restore instead of upload
                existing_backups = deployer.list_backups(app)
                existing_hashes = {b.get("hash"): b for b in existing_backups if b.get("hash")}

                local_in_backups = local_hash in existing_hashes
                remote_needs_backup = remote_hash and remote_hash not in existing_hashes

                if local_in_backups:
                    # Version exists in backups - restore it instead of uploading
                    backup = existing_hashes[local_hash]
                    backup_path = backup["path"]

                    has_service_template = bool(app_config.service_template)
                    total_steps = (1 if remote_needs_backup else 0) + 1 + (1 if has_service_template else 0) + len(services_to_manage) * 2
                    counter = StepCounter(total_steps)

                    click.echo(f"Restoring {app} from backup...")

                    stop_services(service_manager, services_to_manage, counter)

                    if remote_needs_backup:
                        remote_target = f"{target['user']}@{target['host']}:{remote_path}"
                        counter.next(f"Backing up {remote_target}")
                        deployer.backup(app, remote_path)

                    counter.next(f"Restoring {local_hash} from backup")
                    deployer.restore(backup_path, remote_path)

                    service_hash = sync_service_file(
                        ssh, service_manager, app_config, module_config, counter
                    )

                    start_services(service_manager, services_to_manage, counter)

                    history.record(DeploymentRecord(
                        module=module,
                        app=app,
                        binary_hash=local_hash,
                        remote_path=remote_path,
                        backup_path=backup_path,
                        action="push",
                        success=True,
                        service_hash=service_hash,
                    ))
                    click.echo(warning(f"{app} ({local_hash}) restored from backup. Marked as current."))
                    continue

                # Fresh deploy - upload new binary
                has_service_template = bool(app_config.service_template)
                total_steps = (1 if remote_needs_backup else 0) + 1 + (1 if has_service_template else 0) + len(services_to_manage) * 2
                counter = StepCounter(total_steps)

                click.echo(f"Deploying {app}...")

                stop_services(service_manager, services_to_manage, counter)

                remote_target = f"{target['user']}@{target['host']}:{remote_path}"
                backup_path = None

                if remote_needs_backup:
                    counter.next(f"Backing up {remote_target}")
                    backup_path = deployer.backup(app, remote_path)

                counter.next(f"Uploading {local_path}")
                click.echo(f"                {SYMBOLS['arrow']} {remote_target}")
                deployer.deploy(local_path, remote_path)

                service_hash = sync_service_file(
                    ssh, service_manager, app_config, module_config, counter
                )

                start_services(service_manager, services_to_manage, counter)

                history.record(DeploymentRecord(
                    module=module,
                    app=app,
                    binary_hash=local_hash,
                    remote_path=remote_path,
                    backup_path=backup_path,
                    action="push",
                    success=True,
                    service_hash=service_hash,
                ))

                click.echo(success(f"Deployed {app} ({local_hash})"))

    except SSHError as e:
        # Log failed deployment
        history.record(DeploymentRecord(
            module=module,
            app=apps[0] if apps else "",
            binary_hash="",
            remote_path="",
            action="push",
            success=False,
            error_message=str(e),
        ))
        raise SatDeployError(str(e))


@main.command()
@click.option("--module", "-m", required=True, help="Target module")
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def status(module: str, config_dir: Path | None):
    """Show status of deployed apps and services."""
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    try:
        module_config = config.get_module(module)
        target = {"host": module_config.host, "user": module_config.user}
    except KeyError:
        raise SatDeployError(f"Module '{module}' not found in config")
    apps = config.apps
    history = get_history(config_dir)

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

                # Get hash and timestamp from history (only if actually deployed)
                hash_display = "-"
                timestamp_display = "-"
                if deployed:
                    last_deploy = history.get_last_deployment(app_name)
                    if last_deploy and last_deploy.success:
                        hash_display = last_deploy.binary_hash or "-"
                        timestamp_display = format_iso_timestamp(last_deploy.timestamp)

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
                hash_col = f"{hash_display:<10}"
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
@click.option("--module", "-m", required=True, help="Target module")
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def list_backups(app: str, module: str, config_dir: Path | None):
    """List all versions of an app (deployed + backups).

    APP is the name of the application to list versions for.

    Shows the currently deployed version at the top, followed by
    all available backups that can be restored via rollback.
    """
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    app_config = get_app_config_or_error(config, app)

    try:
        module_config = config.get_module(module)
        target = {"host": module_config.host, "user": module_config.user}
    except KeyError:
        raise SatDeployError(f"Module '{module}' not found in config")
    history = get_history(config_dir)

    # Get currently deployed version from history
    last_deploy = history.get_last_deployment(app)

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

            # Show all versions, arrow on deployed one
            for version in versions:
                hash_display = version.get("hash") or "-"
                timestamp_display = version.get("timestamp") or "-"
                is_deployed = hash_display == current_hash

                if is_deployed:
                    bullet = click.style(SYMBOLS["arrow"], fg="green")
                    hash_col = click.style(f"{hash_display:<10}", fg="green")
                    status_col = click.style("deployed", fg="green")
                else:
                    bullet = click.style(SYMBOLS["bullet"], fg="blue")
                    hash_col = click.style(f"{hash_display:<10}", fg="blue")
                    status_col = click.style("backup", fg="blue")

                timestamp_col = click.style(f"{timestamp_display:<20}", fg="bright_black")
                click.echo(f"  {bullet} {hash_col}\t{timestamp_col}\t{status_col}")

        except SSHError as e:
            raise SatDeployError(str(e))


@main.command()
@click.argument("app")
@click.argument("hash", required=False, default=None)
@click.option("--module", "-m", required=True, help="Target module")
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def rollback(app: str, hash: str | None, module: str, config_dir: Path | None):  # noqa: A002
    """Rollback to a previous version.

    APP is the name of the application to rollback.
    HASH is the optional backup hash to restore (defaults to previous version).
    """
    target_hash = hash  # Rename to avoid shadowing builtin
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    app_config = get_app_config_or_error(config, app)

    remote_path = app_config.remote
    service = app_config.service
    try:
        module_config = config.get_module(module)
        target = {"host": module_config.host, "user": module_config.user}
    except KeyError:
        raise SatDeployError(f"Module '{module}' not found in config")
    history = get_history(config_dir)
    backup_path = None

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
@click.argument("app")
@click.option("--module", "-m", required=True, help="Target module")
@click.option(
    "--lines",
    "-n",
    type=int,
    default=100,
    help="Number of lines to show (default: 100)",
)
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def logs(app: str, module: str, lines: int, config_dir: Path | None):
    """Show logs for an app's service.

    APP is the name of the application to show logs for.
    """
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

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

    try:
        module_config = config.get_module(module)
        target = {"host": module_config.host, "user": module_config.user}
    except KeyError:
        raise SatDeployError(f"Module '{module}' not found in config")

    try:
        with SSHClient(host=target["host"], user=target["user"]) as ssh:
            service_manager = ServiceManager(ssh)
            click.echo(click.style(f"Logs for {app} ({service}):", bold=True))
            click.echo("")
            log_output = service_manager.get_logs(service, lines=lines)
            click.echo(log_output)

    except SSHError as e:
        raise SatDeployError(str(e))


@main.group(cls=ColoredGroup)
def fleet():
    """Fleet management commands."""
    pass


def check_module_online(host: str, user: str) -> bool:
    """Check if a module is reachable via SSH.

    Args:
        host: The module's hostname or IP.
        user: The SSH user.

    Returns:
        True if module is reachable, False otherwise.
    """
    try:
        with SSHClient(host=host, user=user) as ssh:
            return True
    except SSHError:
        return False


@fleet.command("status")
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def fleet_status(config_dir: Path | None):
    """Show status of all modules."""
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    modules = config.get_modules()

    if not modules:
        click.echo("No modules configured.")
        return

    click.echo(click.style("Fleet Status", bold=True))
    click.echo("")

    for module_name, module in modules.items():
        online = check_module_online(module.host, module.user)

        if online:
            status_symbol = click.style(SYMBOLS["check"], fg="green")
            status_text = click.style("online", fg="green")
        else:
            status_symbol = click.style(SYMBOLS["cross"], fg="red")
            status_text = click.style("offline", fg="red")

        click.echo(f"  {status_symbol} {module_name:<12} {status_text}")


@main.command()
@click.argument("module1")
@click.argument("module2")
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def diff(module1: str, module2: str, config_dir: Path | None):
    """Compare two modules.

    MODULE1 and MODULE2 are the names of the modules to compare.
    """
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    history = get_history(config_dir)
    state1 = history.get_module_state(module1)
    state2 = history.get_module_state(module2)

    all_apps = set(state1.keys()) | set(state2.keys())

    if not all_apps:
        click.echo("No deployment history for either module.")
        return

    click.echo(click.style(f"Comparing {module1} vs {module2}", bold=True))
    click.echo("")

    # Header
    header = f"    {'APP':<16} {module1:<12} {module2:<12} {'STATUS'}"
    click.echo(click.style(header, fg="bright_black"))
    click.echo(click.style("    " + "-" * 50, fg="bright_black"))

    for app_name in sorted(all_apps):
        hash1 = state1[app_name].binary_hash if app_name in state1 else "-"
        hash2 = state2[app_name].binary_hash if app_name in state2 else "-"

        if hash1 == hash2:
            symbol = click.style(SYMBOLS["check"], fg="green")
            status = click.style("match", fg="green")
        else:
            symbol = click.style(SYMBOLS["cross"], fg="yellow")
            status = click.style("differs", fg="yellow")

        click.echo(f"  {symbol} {app_name:<16} {hash1:<12} {hash2:<12} {status}")


@main.command()
@click.argument("source")
@click.argument("target")
@click.option("--clean-vmem", is_flag=True, help="Clear vmem on target")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def sync(source: str, target: str, clean_vmem: bool, yes: bool, config_dir: Path | None):
    """Sync target module to match source.

    SOURCE is the module to sync from.
    TARGET is the module to sync to.
    """
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.load() is None:
        raise SatDeployError(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    history = get_history(config_dir)

    # Get diff to show what will be synced
    state_source = history.get_module_state(source)
    state_target = history.get_module_state(target)

    all_apps = set(state_source.keys()) | set(state_target.keys())
    apps_to_sync = []

    for app_name in sorted(all_apps):
        hash_source = state_source[app_name].binary_hash if app_name in state_source else None
        hash_target = state_target[app_name].binary_hash if app_name in state_target else None

        if hash_source != hash_target:
            apps_to_sync.append(app_name)

    if not apps_to_sync:
        click.echo(f"Modules {source} and {target} are already in sync.")
        return

    click.echo(click.style(f"Syncing {target} to match {source}", bold=True))
    click.echo("")
    click.echo(f"Apps to sync: {', '.join(apps_to_sync)}")
    if clean_vmem:
        click.echo("Will also clear vmem directories.")
    click.echo("")

    if not yes:
        if not click.confirm("Proceed?"):
            click.echo("Aborted.")
            return

    click.echo(success(f"Sync from {source} to {target} would proceed (not yet implemented)"))
