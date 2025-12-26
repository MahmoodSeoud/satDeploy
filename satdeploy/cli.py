"""CLI entry point for satdeploy."""

import os
from pathlib import Path

import click

from satdeploy.config import DEFAULT_CONFIG_DIR, Config
from satdeploy.deployer import Deployer
from satdeploy.services import ServiceManager, ServiceStatus
from satdeploy.ssh import SSHClient


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

    click.echo("Setting up satdeploy configuration...")
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
    click.echo(f"Config saved to {config.config_path}")


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
    click.echo(f"Connecting to {target['host']}...")

    with SSHClient(host=target["host"], user=target["user"]) as ssh:
        service_manager = ServiceManager(ssh)
        deployer = Deployer(
            ssh=ssh,
            backup_dir=config.backup_dir,
            max_backups=config.max_backups,
        )

        click.echo(f"Deploying {app}...")

        if service:
            click.echo(f"  Stopping {service}...")

        result = deployer.push(
            app_name=app,
            local_path=local_path,
            remote_path=remote_path,
            service=service,
            service_manager=service_manager,
        )

        if not result.success:
            raise click.ClickException(f"Deployment failed: {result.error_message}")

        click.echo(f"  Uploaded {local_path} -> {remote_path}")

        if service:
            click.echo(f"  Starting {service}...")
            if result.health_check_passed:
                click.echo(f"  Health check passed")
            else:
                click.echo(f"  Warning: Health check failed")

        click.echo(f"Successfully deployed {app} ({result.binary_hash})")


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

    click.echo(f"Target: {target['host']} ({target['user']})")
    click.echo("")

    if not apps:
        click.echo("No apps configured.")
        return

    with SSHClient(host=target["host"], user=target["user"]) as ssh:
        service_manager = ServiceManager(ssh)

        for app_name, app_config in apps.items():
            service = app_config.get("service")
            remote_path = app_config.get("remote")

            if service:
                svc_status = service_manager.get_status(service)
                if svc_status == ServiceStatus.RUNNING:
                    status_str = "running"
                elif svc_status == ServiceStatus.STOPPED:
                    status_str = "stopped"
                elif svc_status == ServiceStatus.FAILED:
                    status_str = "failed"
                else:
                    status_str = "unknown"
                click.echo(f"  {app_name}: {status_str} ({service})")
            else:
                deployed = ssh.file_exists(remote_path)
                status_str = "deployed" if deployed else "not deployed"
                click.echo(f"  {app_name}: {status_str} (library)")
