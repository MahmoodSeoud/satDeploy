"""CLI entry point for satdeploy."""

import os
from datetime import datetime
from pathlib import Path

import click

from satdeploy.config import DEFAULT_CONFIG_DIR, Config
from satdeploy.dependencies import DependencyResolver
from satdeploy.deployer import Deployer
from satdeploy.history import DeploymentRecord, History
from satdeploy.output import success, error, warning, info, step, SYMBOLS
from satdeploy.services import ServiceManager, ServiceStatus
from satdeploy.ssh import SSHClient, SSHError


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
        click.ClickException: If cyclic dependencies are detected.
    """
    resolver = DependencyResolver(config.apps)

    if resolver.has_cycle():
        raise click.ClickException("Cyclic dependency detected in config")

    # For libraries with restart list, use that
    restart_apps = resolver.get_restart_apps(app)
    if restart_apps:
        services = []
        for restart_app in restart_apps:
            restart_config = config.get_app(restart_app)
            if restart_config and restart_config.get("service"):
                services.append((restart_app, restart_config.get("service")))
        return services

    # For services with dependencies, get the full stop order
    if service:
        stop_order = resolver.get_stop_order(app)
        services = []
        for dep_app in stop_order:
            dep_config = config.get_app(dep_app)
            if dep_config and dep_config.get("service"):
                services.append((dep_app, dep_config.get("service")))
        return services

    return []


def get_app_config_or_error(config: Config, app: str) -> dict:
    """Get app configuration or raise ClickException if not found.

    Args:
        config: The loaded configuration.
        app: The app name to look up.

    Returns:
        The app configuration dict.

    Raises:
        click.ClickException: If the app is not found in config.
    """
    app_config = config.get_app(app)
    if app_config is None:
        raise click.ClickException(
            f"App '{app}' not found in config. Check your config.yaml."
        )
    return app_config


@click.group()
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
    host = click.prompt("Target host (IP or hostname)")
    user = click.prompt("Target user", default="root")

    data = {
        "target": {
            "host": host,
            "user": user,
        },
        "backup_dir": "/opt/satdeploy/backups",
        "max_backups": 10,
        "apps": {},
    }

    config.save(data)
    click.echo("")
    click.echo(success(f"Config saved to {config.config_path}"))


@main.command()
@click.argument("app")
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
def push(app: str, local: str | None, config_dir: Path | None):
    """Deploy a binary to the target.

    APP is the name of the application to deploy (as defined in config.yaml).
    """
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.load() is None:
        raise click.ClickException(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    app_config = config.get_app(app)
    if app_config is None:
        raise click.ClickException(
            f"App '{app}' not found in config. Check your config.yaml."
        )

    local_path = os.path.expanduser(local or app_config.get("local"))
    remote_path = app_config.get("remote")
    service = app_config.get("service")

    if not os.path.exists(local_path):
        raise click.ClickException(f"Local file not found: {local_path}")

    target = config.target
    history = get_history(config_dir)
    binary_hash = None

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

            # Calculate total steps: backup + deploy + stop services + start services
            num_services = len(services_to_manage)
            total_steps = 2 + num_services * 2  # backup, deploy, stop each, start each
            current_step = 0

            click.echo(f"Deploying {app}...")

            # Stop services in order
            for svc_app, svc_name in services_to_manage:
                current_step += 1
                click.echo(step(current_step, total_steps, f"Stopping {svc_app} ({svc_name})"))
                service_manager.stop(svc_name)

            # Backup and deploy
            remote_target = f"{target['user']}@{target['host']}:{remote_path}"

            current_step += 1
            click.echo(step(current_step, total_steps, f"Backing up {remote_target}"))
            backup_path = deployer.backup(app, remote_path)

            current_step += 1
            click.echo(step(current_step, total_steps, f"Uploading {local_path}"))
            click.echo(f"                {SYMBOLS['arrow']} {remote_target}")
            deployer.deploy(local_path, remote_path)
            binary_hash = deployer.compute_hash(local_path)

            # Start services in reverse order
            for svc_app, svc_name in reversed(services_to_manage):
                current_step += 1
                click.echo(step(current_step, total_steps, f"Starting {svc_app} ({svc_name})"))
                service_manager.start(svc_name)
                if service_manager.is_healthy(svc_name):
                    click.echo(success(f"Health check passed for {svc_app}"))
                else:
                    click.echo(warning(f"Health check failed for {svc_app}"))

            # Log successful deployment
            history.record(DeploymentRecord(
                app=app,
                binary_hash=binary_hash,
                remote_path=remote_path,
                backup_path=backup_path,
                action="push",
                success=True,
            ))

            click.echo(success(f"Deployed {app} ({binary_hash})"))

    except SSHError as e:
        # Log failed deployment
        history.record(DeploymentRecord(
            app=app,
            binary_hash=binary_hash or "",
            remote_path=remote_path,
            action="push",
            success=False,
            error_message=str(e),
        ))
        raise click.ClickException(str(e))


@main.command()
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def status(config_dir: Path | None):
    """Show status of deployed apps and services."""
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.load() is None:
        raise click.ClickException(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    target = config.target
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
        raise click.ClickException(str(e))


@main.command("list")
@click.argument("app")
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def list_backups(app: str, config_dir: Path | None):
    """List all versions of an app (deployed + backups).

    APP is the name of the application to list versions for.

    Shows the currently deployed version at the top, followed by
    all available backups that can be restored via rollback.
    """
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.load() is None:
        raise click.ClickException(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    app_config = config.get_app(app)
    if app_config is None:
        raise click.ClickException(
            f"App '{app}' not found in config. Check your config.yaml."
        )

    target = config.target
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
            if current_hash and current_hash not in seen_keys:
                timestamp_display = format_iso_timestamp(last_deploy.timestamp)
                seen_keys[current_hash] = {
                    "hash": current_hash,
                    "timestamp": timestamp_display,
                }

            # Build unified list of unique versions
            versions = list(seen_keys.values())

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
            raise click.ClickException(str(e))


@main.command()
@click.argument("app")
@click.argument("version", required=False, default=None)
@click.option(
    "--config-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Config directory (default: ~/.satdeploy)",
)
def rollback(app: str, version: str | None, config_dir: Path | None):
    """Rollback to a previous version.

    APP is the name of the application to rollback.
    VERSION is the optional backup version to restore (defaults to latest).
    """
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.load() is None:
        raise click.ClickException(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    app_config = config.get_app(app)
    if app_config is None:
        raise click.ClickException(
            f"App '{app}' not found in config. Check your config.yaml."
        )

    remote_path = app_config.get("remote")
    service = app_config.get("service")
    target = config.target
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
            backups = deployer.list_backups(app)
            if not backups:
                raise click.ClickException("No backups available for rollback")

            # Get currently deployed hash to filter it out
            last_deploy = history.get_last_deployment(app)
            current_hash = last_deploy.binary_hash if last_deploy and last_deploy.success else None

            if version:
                matching = [b for b in backups if b["version"] == version]
                if not matching:
                    raise click.ClickException(f"Version {version} not found")
                backup = matching[0]
            elif current_hash:
                # Filter out the currently deployed version
                available = [b for b in backups if b.get("hash") != current_hash]
                if not available:
                    raise click.ClickException("No different backup available for rollback")
                backup = available[0]
            else:
                # No history, just use the most recent backup
                backup = backups[0]

            backup_path = backup["path"]
            backup_hash = backup.get("hash") or "-"
            backup_timestamp = backup.get("timestamp") or "-"

            # Check if current version needs to be backed up (not already in backups)
            backup_hashes = {b.get("hash") for b in backups if b.get("hash")}
            needs_backup = current_hash and current_hash not in backup_hashes

            # Calculate total steps
            num_services = len(services_to_manage)
            total_steps = (1 if needs_backup else 0) + 1 + num_services * 2
            current_step = 0

            click.echo(f"Rolling back {app}...")

            # Stop services in order
            for svc_app, svc_name in services_to_manage:
                current_step += 1
                click.echo(step(current_step, total_steps, f"Stopping {svc_app} ({svc_name})"))
                service_manager.stop(svc_name)

            # Backup current version only if not already in backups
            if needs_backup:
                remote_target = f"{target['user']}@{target['host']}:{remote_path}"
                current_step += 1
                click.echo(step(current_step, total_steps, f"Backing up {remote_target}"))
                deployer.backup(app, remote_path)

            # Restore the backup
            current_step += 1
            click.echo(step(current_step, total_steps, f"Restoring {backup_hash} ({backup_timestamp})"))
            ssh.run(f"cp '{backup_path}' '{remote_path}'")
            ssh.run(f"chmod +x '{remote_path}'")

            # Start services in reverse order
            for svc_app, svc_name in reversed(services_to_manage):
                current_step += 1
                click.echo(step(current_step, total_steps, f"Starting {svc_app} ({svc_name})"))
                service_manager.start(svc_name)
                if service_manager.is_healthy(svc_name):
                    click.echo(success(f"Health check passed for {svc_app}"))
                else:
                    click.echo(warning(f"Health check failed for {svc_app}"))

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
        raise click.ClickException(str(e))


@main.command()
@click.argument("app")
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
def logs(app: str, lines: int, config_dir: Path | None):
    """Show logs for an app's service.

    APP is the name of the application to show logs for.
    """
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    config = Config(config_dir=config_dir)

    if config.load() is None:
        raise click.ClickException(
            f"Config not found at {config.config_path}. Run 'satdeploy init' first."
        )

    app_config = config.get_app(app)
    if app_config is None:
        raise click.ClickException(
            f"App '{app}' not found in config. Check your config.yaml."
        )

    service = app_config.get("service")
    if not service:
        raise click.ClickException(
            f"App '{app}' is a library and has no service. Cannot show logs."
        )

    target = config.target

    try:
        with SSHClient(host=target["host"], user=target["user"]) as ssh:
            service_manager = ServiceManager(ssh)
            click.echo(click.style(f"Logs for {app} ({service}):", bold=True))
            click.echo("")
            log_output = service_manager.get_logs(service, lines=lines)
            click.echo(log_output)

    except SSHError as e:
        raise click.ClickException(str(e))
