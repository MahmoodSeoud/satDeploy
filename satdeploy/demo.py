"""Demo mode for satdeploy — simulated satellite target via Docker.

Uses the repo's docker-compose.yml to start zmqproxy + agent, then
writes a demo config pointing at the running containers. When run from
a git clone, uses the existing dev compose setup. When installed via pip
(no docker-compose.yml), falls back to pulling a pre-built GHCR image.
"""

import shutil
import subprocess
import time
from pathlib import Path

import click
import yaml

from satdeploy.output import success, warning, SatDeployError
from satdeploy.transport.csp import CSPTransport
from satdeploy.transport.base import TransportError


DEMO_CONFIG_PATH = Path.home() / ".satdeploy" / ".demo-config.yaml"
DEMO_DIR = Path.home() / ".satdeploy" / "demo"
GHCR_IMAGE = "ghcr.io/mahmoodseoud/satdeploy-sim:latest"

# Demo satellite configuration — matches agent defaults
DEMO_AGENT_NODE = 5425
DEMO_GROUND_NODE = 40
DEMO_ZMQ_PUB_PORT = 9600
DEMO_ZMQ_SUB_PORT = 9601

# Embedded compose for standalone mode (no repo checkout)
STANDALONE_COMPOSE = """\
services:
  zmqproxy:
    image: {image}
    ports:
      - "{pub_port}:{pub_port}"
      - "{sub_port}:{sub_port}"
    command: zmqproxy -s tcp://0.0.0.0:{pub_port} -p tcp://0.0.0.0:{sub_port}
    restart: unless-stopped

  agent:
    image: {image}
    command: satdeploy-agent -i ZMQ -p zmqproxy -S {pub_port} -P {sub_port}
    depends_on:
      zmqproxy:
        condition: service_started
    restart: unless-stopped
"""

DEMO_CONFIG = {
    "name": "demo-satellite",
    "transport": "csp",
    "zmq_endpoint": f"tcp://localhost:{DEMO_ZMQ_PUB_PORT}",
    "agent_node": DEMO_AGENT_NODE,
    "ground_node": DEMO_GROUND_NODE,
    "zmq_pub_port": DEMO_ZMQ_PUB_PORT,
    "zmq_sub_port": DEMO_ZMQ_SUB_PORT,
    "backup_dir": "/opt/satdeploy/backups",
    "max_backups": 5,
    "apps": {
        "test_app": {
            "local": str(DEMO_DIR / "binaries" / "test_app"),
            "remote": "/opt/demo/bin/test_app",
            "service": None,
            "param": None,
        }
    },
}

TUTORIAL_TEXT = """\

  You just started a simulated satellite running the satdeploy agent.

  {line} Step 1: Watch the satellite {line2}

    Open a second terminal and run:
    $ satdeploy demo watch

    This shows what the satellite agent receives in real time.
    Keep it open — you'll see every command arrive.

  {line} Step 2: Open the ground station {line3}

    Open a third terminal and launch the CSH ground station:
    $ docker compose run csh

    CSH connects to the satellite automatically. The default
    target node (5425) is already set. Try these commands:

      csh> satdeploy status
      csh> satdeploy deploy test_app
      csh> satdeploy list test_app
      csh> satdeploy rollback test_app
      csh> satdeploy verify test_app

    Watch the satellite terminal — you'll see each command
    arrive, the backup, download, and install happen live.

  {line} Scripted deploys (Python CLI) {line4}

    For CI/automation, satdeploy also has a Python CLI:
    $ satdeploy deploy test_app --config ~/.satdeploy/.demo-config.yaml
    $ satdeploy status --config ~/.satdeploy/.demo-config.yaml

  {line} When you're done {line5}

    Stop the demo:  satdeploy demo stop
    Use with real hardware:  satdeploy demo eject
"""


def _repo_root() -> Path:
    """Get the repo root (parent of satdeploy/ package)."""
    return Path(__file__).parent.parent


def _find_repo_compose() -> Path | None:
    """Find the repo's docker-compose.yml if we're in a git checkout."""
    compose = _repo_root() / "docker-compose.yml"
    return compose if compose.exists() else None


def _find_demo_binary(version: str) -> Path:
    """Find the demo binary for a given version (v1 or v2)."""
    demo_path = _repo_root() / "demo" / version / "test_app"
    if demo_path.exists():
        return demo_path
    raise SatDeployError(
        f"Demo binary not found at {demo_path}. "
        "Re-clone or reinstall satdeploy."
    )


def _check_docker() -> None:
    """Verify Docker is installed and running."""
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise SatDeployError(
                "Docker Compose v2 not found. Install Docker Desktop: "
                "https://www.docker.com/products/docker-desktop/"
            )
    except FileNotFoundError:
        raise SatDeployError(
            "Docker not found. Install Docker Desktop: "
            "https://www.docker.com/products/docker-desktop/"
        )

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            raise SatDeployError(
                "Docker daemon is not running. Start Docker Desktop and try again."
            )
    except subprocess.TimeoutExpired:
        raise SatDeployError(
            "Docker daemon timed out. Start Docker Desktop and try again."
        )


def _get_compose_file() -> Path:
    """Get the compose file used by the demo (repo or standalone)."""
    # Check for repo compose first
    repo_compose = _find_repo_compose()
    if repo_compose:
        return repo_compose
    # Standalone mode uses the demo dir
    return DEMO_DIR / "docker-compose.yml"


def _is_agent_container_running(compose_file: Path) -> bool:
    """Check if the agent container is running via docker compose."""
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "ps",
         "--status", "running", "--format", "{{.Name}}"],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0 and "agent" in result.stdout


def _ensure_agent_dirs(compose_file: Path) -> None:
    """Create required directories and pre-install v1 inside the agent container."""
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file),
         "exec", "-T", "agent", "mkdir", "-p",
         "/opt/demo/bin", "/opt/satdeploy/backups"],
        capture_output=True, text=True, timeout=10,
    )
    # Pre-install v1 on the "satellite" so rollback has something to restore
    v1_binary = _find_demo_binary("v1")
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file),
         "cp", str(v1_binary), "agent:/opt/demo/bin/test_app"],
        capture_output=True, text=True, timeout=10,
    )
    subprocess.run(
        ["docker", "compose", "-f", str(compose_file),
         "exec", "-T", "agent", "chmod", "755", "/opt/demo/bin/test_app"],
        capture_output=True, text=True, timeout=10,
    )


def _write_demo_config() -> None:
    """Write the demo config YAML."""
    DEMO_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DEMO_CONFIG_PATH, "w") as f:
        yaml.dump(DEMO_CONFIG, f, default_flow_style=False)


def _copy_demo_binary() -> None:
    """Copy v2 demo binary to the demo binaries directory."""
    source = _find_demo_binary("v2")
    dest_dir = DEMO_DIR / "binaries"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "test_app"
    shutil.copy2(source, dest)
    dest.chmod(0o755)


def _wait_for_agent(max_attempts: int = 15, interval: float = 2.0) -> bool:
    """Wait for the demo agent to respond. Connect once, poll in loop."""
    click.echo("Waiting for agent...")
    transport = CSPTransport(
        zmq_endpoint=f"tcp://localhost:{DEMO_ZMQ_PUB_PORT}",
        agent_node=DEMO_AGENT_NODE,
        ground_node=DEMO_GROUND_NODE,
        backup_dir="/opt/satdeploy/backups",
        zmq_pub_port=DEMO_ZMQ_PUB_PORT,
        zmq_sub_port=DEMO_ZMQ_SUB_PORT,
    )
    try:
        transport.connect()
    except (TransportError, Exception):
        return False

    try:
        for attempt in range(max_attempts):
            try:
                result = transport.get_status()
                if isinstance(result, dict):
                    return True
            except TransportError:
                pass
            time.sleep(interval)
    finally:
        transport.disconnect()

    return False


def _print_tutorial() -> None:
    """Print the guided tutorial output."""
    line = "\u2500" * 3
    click.echo(TUTORIAL_TEXT.format(
        line=line,
        line2="\u2500" * 29,
        line3="\u2500" * 26,
        line4="\u2500" * 22,
        line5="\u2500" * 31,
    ))


def _start_with_repo_compose(compose_file: Path) -> None:
    """Start the demo using the repo's docker-compose.yml.

    This is the fast path — the satdev image is already built locally,
    so `docker compose up -d` takes seconds, not minutes.
    """
    # Check if containers are already running
    if _is_agent_container_running(compose_file):
        click.echo(success("Docker containers already running"))
    else:
        click.echo("Starting containers (using repo docker-compose.yml)...")
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "-d"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            if "port is already allocated" in result.stderr.lower() or \
               "address already in use" in result.stderr.lower():
                raise SatDeployError(
                    f"Port {DEMO_ZMQ_PUB_PORT} or {DEMO_ZMQ_SUB_PORT} is already in use. "
                    f"Check with: lsof -i :{DEMO_ZMQ_PUB_PORT}"
                )
            raise SatDeployError(f"Failed to start containers: {result.stderr}")
        click.echo(success("Docker containers started"))

    # Create demo directories inside agent container
    _ensure_agent_dirs(compose_file)


def _start_standalone() -> None:
    """Start the demo without the repo (standalone/pip install mode).

    Pulls a pre-built GHCR image or builds Dockerfile.sim locally.
    """
    # Try GHCR pull
    click.echo("Pulling simulator image...")
    result = subprocess.run(
        ["docker", "pull", GHCR_IMAGE],
        capture_output=True, text=True, timeout=300,
    )

    if result.returncode == 0:
        image = GHCR_IMAGE
    else:
        # Fall back to local Dockerfile.sim build
        dockerfile = _repo_root() / "Dockerfile.sim"
        if not dockerfile.exists():
            raise SatDeployError(
                "Cannot start demo: no pre-built image available and "
                "Dockerfile.sim not found. Clone the repo and try again."
            )
        click.echo(warning(
            "Pre-built image not available. Building locally — "
            "this may take ~5 minutes on first run."
        ))
        local_image = "satdeploy-sim:local"
        result = subprocess.run(
            ["docker", "build", "-t", local_image,
             "-f", str(dockerfile), str(_repo_root())],
            timeout=600,
        )
        if result.returncode != 0:
            raise SatDeployError("Failed to build simulator image locally.")
        image = local_image

    # Write standalone compose file
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    compose_path = DEMO_DIR / "docker-compose.yml"
    compose_path.write_text(STANDALONE_COMPOSE.format(
        image=image,
        pub_port=DEMO_ZMQ_PUB_PORT,
        sub_port=DEMO_ZMQ_SUB_PORT,
    ))

    # Start containers
    click.echo("Starting containers...")
    result = subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "up", "-d"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise SatDeployError(f"Failed to start containers: {result.stderr}")
    click.echo(success("Docker containers started"))


def demo_start() -> None:
    """Start the demo environment.

    Two modes:
    1. Repo checkout: uses the existing docker-compose.yml (fast — image already built)
    2. Standalone: pulls GHCR image or builds Dockerfile.sim (slow first run)
    """
    _check_docker()

    # Check if demo is already fully set up and running
    compose_file = _get_compose_file()
    if compose_file.exists() and _is_agent_container_running(compose_file):
        if DEMO_CONFIG_PATH.exists():
            click.echo(success("Demo already running"))
            _print_tutorial()
            return

    # Start containers
    repo_compose = _find_repo_compose()
    if repo_compose:
        _start_with_repo_compose(repo_compose)
        compose_file = repo_compose
    else:
        _start_standalone()
        compose_file = DEMO_DIR / "docker-compose.yml"

    # Write demo config
    _write_demo_config()

    # Copy demo binary (v2 = the "new version" user will deploy)
    _copy_demo_binary()

    # Wait for agent readiness
    if _wait_for_agent():
        click.echo(success(f"Agent responding on CSP node {DEMO_AGENT_NODE}"))
    else:
        logs_result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "logs", "agent",
             "--tail", "20"],
            capture_output=True, text=True, timeout=10,
        )
        click.echo(warning("Agent did not respond within 30 seconds."))
        if logs_result.stdout:
            click.echo("Agent logs:")
            click.echo(logs_result.stdout[-500:])
        raise SatDeployError(
            "Demo agent failed to start. Check Docker logs above."
        )

    click.echo(success(f"Demo config written to {DEMO_CONFIG_PATH}"))
    _print_tutorial()


def demo_stop(clean: bool = False) -> None:
    """Stop the demo environment."""
    compose_file = _get_compose_file()
    repo_compose = _find_repo_compose()

    if repo_compose and compose_file == repo_compose:
        # Using repo compose — don't stop dev containers, just remove demo config
        click.echo("Demo uses repo docker-compose.yml — leaving containers running.")
        click.echo("Stop them with: docker compose down")
    elif compose_file.exists():
        click.echo("Stopping demo containers...")
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "down"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            click.echo(success("Demo containers stopped"))
        else:
            click.echo(warning(f"docker compose down failed: {result.stderr}"))
    else:
        click.echo("Demo environment is not running.")

    if clean:
        if DEMO_DIR.exists():
            shutil.rmtree(DEMO_DIR)
        if DEMO_CONFIG_PATH.exists():
            DEMO_CONFIG_PATH.unlink()
        # Clean up demo history too
        history_path = DEMO_CONFIG_PATH.parent / ".demo-history.db"
        if history_path.exists():
            history_path.unlink()
        click.echo(success("Removed demo files"))
    elif DEMO_CONFIG_PATH.exists():
        # Always clean up demo config so next `demo start` re-initializes
        DEMO_CONFIG_PATH.unlink()
        click.echo(success("Demo config removed"))


def demo_status() -> None:
    """Show demo environment status."""
    compose_file = _get_compose_file()

    if not compose_file.exists() or not _is_agent_container_running(compose_file):
        click.echo("Demo environment is not running.")
        click.echo("Start with: satdeploy demo start")
        return

    click.echo(success("Demo environment is running"))
    click.echo(f"  Agent:     CSP node {DEMO_AGENT_NODE}")
    click.echo(f"  ZMQ proxy: localhost:{DEMO_ZMQ_PUB_PORT}/{DEMO_ZMQ_SUB_PORT}")
    click.echo(f"  Config:    {DEMO_CONFIG_PATH}")

    repo_compose = _find_repo_compose()
    if repo_compose:
        click.echo(f"  Mode:      repo (using {repo_compose})")
    else:
        click.echo(f"  Mode:      standalone")


def demo_watch() -> None:
    """Stream the satellite agent's logs in real time.

    Runs `docker compose logs -f agent` so the user can see what the
    satellite does when it receives deploy/rollback/status commands.
    Ctrl+C to stop watching.
    """
    compose_file = _get_compose_file()

    if not compose_file.exists() or not _is_agent_container_running(compose_file):
        raise SatDeployError(
            "Demo environment is not running. Start with: satdeploy demo start"
        )

    click.echo("Watching satellite agent logs (Ctrl+C to stop)...")
    click.echo("")
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(compose_file),
             "logs", "-f", "--tail", "20", "agent"],
            timeout=None,
        )
    except KeyboardInterrupt:
        click.echo("\nStopped watching.")


def demo_eject() -> None:
    """Generate a real config template from the demo."""
    transport = click.prompt(
        "What transport will you use?",
        type=click.Choice(["ssh", "csp"]),
        default="csp",
    )

    target_dir = Path.home() / ".satdeploy"
    target_config = target_dir / "config.yaml"

    if target_config.exists():
        click.echo(warning(f"Config already exists at {target_config}"))
        if not click.confirm("Overwrite?"):
            click.echo("Aborted.")
            return

    target_dir.mkdir(parents=True, exist_ok=True)

    if transport == "csp":
        template = {
            "name": "my-satellite",
            "transport": "csp",
            "zmq_endpoint": "tcp://localhost:9600",
            "agent_node": 5425,
            "ground_node": 40,
            "zmq_pub_port": 9600,
            "zmq_sub_port": 9601,
            "backup_dir": "/opt/satdeploy/backups",
            "max_backups": 10,
            "apps": {
                "my_app": {
                    "local": "./build/my_app",
                    "remote": "/opt/app/bin/my_app",
                    "service": None,
                    "param": None,
                }
            },
        }
    else:
        template = {
            "name": "my-target",
            "transport": "ssh",
            "host": "192.168.1.100",
            "user": "root",
            "backup_dir": "/opt/satdeploy/backups",
            "max_backups": 10,
            "apps": {
                "my_app": {
                    "local": "./build/my_app",
                    "remote": "/opt/app/bin/my_app",
                    "service": "my_app.service",
                }
            },
        }

    header = (
        f"# satdeploy config for {transport.upper()} transport\n"
        f"# Generated by: satdeploy demo eject\n"
        f"# Edit the values below for your hardware.\n\n"
    )
    with open(target_config, "w") as f:
        f.write(header)
        yaml.dump(template, f, default_flow_style=False)

    click.echo(success(f"Config template written to {target_config}"))
    click.echo("Edit it for your hardware, then run: satdeploy status")
