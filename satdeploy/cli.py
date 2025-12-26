"""CLI entry point for satdeploy."""

from pathlib import Path

import click

from satdeploy.config import DEFAULT_CONFIG_DIR, Config


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
