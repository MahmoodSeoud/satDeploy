"""CLI entry point for satdeploy."""

import os
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

            # Resolve dependencies
            resolver = DependencyResolver(config.apps)

            # Check for cycles
            if resolver.has_cycle():
                raise click.ClickException("Cyclic dependency detected in config")

            # For libraries with restart list, use that
            restart_apps = resolver.get_restart_apps(app)
            if restart_apps:
                # Library with restart list
                services_to_manage = []
                for restart_app in restart_apps:
                    restart_config = config.get_app(restart_app)
                    if restart_config and restart_config.get("service"):
                        services_to_manage.append(
                            (restart_app, restart_config.get("service"))
                        )
            elif service:
                # Service with dependencies
                stop_order = resolver.get_stop_order(app)
                services_to_manage = []
                for dep_app in stop_order:
                    dep_config = config.get_app(dep_app)
                    if dep_config and dep_config.get("service"):
                        services_to_manage.append(
                            (dep_app, dep_config.get("service"))
                        )
            else:
                services_to_manage = []

            # Calculate total steps: backup + deploy + stop services + start services + health checks
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
            current_step += 1
            click.echo(step(current_step, total_steps, f"Backing up {remote_path}"))
            backup_path = deployer.backup(app, remote_path)

            current_step += 1
            click.echo(step(current_step, total_steps, f"Uploading {local_path} {SYMBOLS['arrow']} {remote_path}"))
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
    header = f"    {'APP':<16} {'STATUS':<14}\t{'VERSION'}"
    click.echo(click.style(header, fg="bright_black"))
    click.echo(click.style("    " + "-" * 50, fg="bright_black"))

    try:
        with SSHClient(host=target["host"], user=target["user"]) as ssh:
            service_manager = ServiceManager(ssh)

            for app_name, app_config in apps.items():
                service = app_config.get("service")
                remote_path = app_config.get("remote")

                # Get version from history
                last_deploy = history.get_last_deployment(app_name)
                if last_deploy and last_deploy.success and last_deploy.backup_path:
                    # Extract version from backup path (e.g., "20240115-143022" from ".../20240115-143022.bak")
                    backup_filename = os.path.basename(last_deploy.backup_path)
                    version_display = backup_filename.replace(".bak", "")
                else:
                    version_display = "-"

                if service:
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
                    deployed = ssh.file_exists(remote_path)
                    if deployed:
                        symbol = click.style(SYMBOLS["bullet"], fg="green")
                        status_text = "deployed"
                        status_color = "green"
                    else:
                        symbol = click.style(SYMBOLS["bullet"], fg="yellow")
                        status_text = "not deployed"
                        status_color = "yellow"

                # Pad plain text first, then colorize
                name_col = f"{app_name:<16}"
                status_col = f"{status_text:<14}"
                version_col = version_display

                click.echo(
                    f"  {symbol} {name_col}"
                    f"{click.style(status_col, fg=status_color)}\t"
                    f"{click.style(version_col, fg='white')}"
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
    """Show available backups for an app.

    APP is the name of the application to list backups for.
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
    current_backup_path = last_deploy.backup_path if last_deploy and last_deploy.success else None

    with SSHClient(host=target["host"], user=target["user"]) as ssh:
        deployer = Deployer(
            ssh=ssh,
            backup_dir=config.backup_dir,
            max_backups=config.max_backups,
        )

        try:
            backups = deployer.list_backups(app)

            if not backups:
                click.echo(f"No backups found for {app}.")
                return

            click.echo(click.style(f"Backups for {app}:", bold=True))
            click.echo("")
            # Print header
            header = f"    {'VERSION':<18} {'TIMESTAMP'}"
            click.echo(click.style(header, fg="bright_black"))
            click.echo(click.style("    " + "-" * 40, fg="bright_black"))
            for backup in backups:
                # Check if this is the currently deployed version
                is_current = current_backup_path and backup["version"] in current_backup_path

                if is_current:
                    bullet = click.style(SYMBOLS["arrow"], fg="green")
                    version = click.style(backup["version"], fg="green")
                else:
                    bullet = click.style(SYMBOLS["bullet"], fg="blue")
                    version = click.style(backup["version"], fg="blue")

                timestamp = click.style(backup["timestamp"], fg="bright_black")
                click.echo(f"  {bullet} {version}  {timestamp}")

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

            # Resolve dependencies
            resolver = DependencyResolver(config.apps)

            # Check for cycles
            if resolver.has_cycle():
                raise click.ClickException("Cyclic dependency detected in config")

            # For libraries with restart list, use that
            restart_apps = resolver.get_restart_apps(app)
            if restart_apps:
                # Library with restart list
                services_to_manage = []
                for restart_app in restart_apps:
                    restart_config = config.get_app(restart_app)
                    if restart_config and restart_config.get("service"):
                        services_to_manage.append(
                            (restart_app, restart_config.get("service"))
                        )
            elif service:
                # Service with dependencies
                stop_order = resolver.get_stop_order(app)
                services_to_manage = []
                for dep_app in stop_order:
                    dep_config = config.get_app(dep_app)
                    if dep_config and dep_config.get("service"):
                        services_to_manage.append(
                            (dep_app, dep_config.get("service"))
                        )
            else:
                services_to_manage = []

            # Calculate total steps: restore + stop services + start services
            num_services = len(services_to_manage)
            total_steps = 1 + num_services * 2  # restore, stop each, start each
            current_step = 0

            click.echo(f"Rolling back {app}...")

            # Stop services in order
            for svc_app, svc_name in services_to_manage:
                current_step += 1
                click.echo(step(current_step, total_steps, f"Stopping {svc_app} ({svc_name})"))
                service_manager.stop(svc_name)

            # Get backup list and find the right one
            backups = deployer.list_backups(app)
            if not backups:
                raise click.ClickException("No backups available for rollback")

            if version:
                matching = [b for b in backups if b["version"] == version]
                if not matching:
                    raise click.ClickException(f"Version {version} not found")
                backup = matching[0]
            else:
                backup = backups[0]

            backup_path = backup["path"]
            version_str = backup["version"]

            # Restore the backup
            current_step += 1
            click.echo(step(current_step, total_steps, f"Restoring {version_str}"))
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
                binary_hash=version_str,
                remote_path=remote_path,
                backup_path=backup_path,
                action="rollback",
                success=True,
            ))

            click.echo(success(f"Rolled back {app} to {version_str}"))

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
