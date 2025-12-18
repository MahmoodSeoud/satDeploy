#!/usr/bin/env python3
"""sat: CLI tool for fast binary deployment to flatsat.

Usage:
    sat status                          Show status of all services
    sat deploy <service> <binary>       Upload and deploy a binary
    sat rollback <service>              Rollback to previous version
    sat restart <service>               Restart service and dependents
    sat logs <service>                  Tail service logs (Ctrl+C to exit)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml


DEFAULT_CONFIG_PATH = 'config.yaml'

# Terminal output symbols
CHECK = '[+]'
CROSS = '[x]'
ARROW = '[~]'


def load_config(config_path=None):
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, uses SAT_CONFIG
                     env var or falls back to DEFAULT_CONFIG_PATH.

    Returns:
        dict: Configuration dictionary.

    Raises:
        FileNotFoundError: If config file doesn't exist.
    """
    if config_path is None:
        config_path = os.environ.get('SAT_CONFIG', DEFAULT_CONFIG_PATH)

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        return yaml.safe_load(f)


def ssh_run(config, command):
    """Run a command on the flatsat via SSH.

    Args:
        config: Configuration dictionary with flatsat host/user.
        command: Command to run on the remote host.

    Returns:
        tuple: (stdout, stderr, returncode)
    """
    flatsat = config.get('flatsat', {})
    host = flatsat.get('host', 'flatsat-disco.local')
    user = flatsat.get('user', 'root')

    ssh_cmd = ['ssh', f'{user}@{host}', command]

    result = subprocess.run(
        ssh_cmd,
        capture_output=True,
        text=True
    )

    return result.stdout, result.stderr, result.returncode


def rsync_upload(config, local_path, service):
    """Upload a binary to the flatsat using rsync.

    Args:
        config: Configuration dictionary.
        local_path: Path to local binary file.
        service: Service name to deploy to.

    Returns:
        tuple: (success, error_message)
    """
    flatsat = config.get('flatsat', {})
    host = flatsat.get('host', 'flatsat-disco.local')
    user = flatsat.get('user', 'root')

    services = config.get('services', {})
    service_config = services.get(service, {})
    remote_path = service_config.get('binary', '')

    if not remote_path:
        return False, f"No binary path configured for service: {service}"

    # Upload as .new file
    remote_dest = f'{user}@{host}:{remote_path}.new'

    rsync_cmd = ['rsync', '-az', '--progress', local_path, remote_dest]

    result = subprocess.run(
        rsync_cmd,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        return False, result.stderr

    return True, None


def cmd_status(config):
    """Execute the status command.

    Args:
        config: Configuration dictionary.

    Returns:
        int: Exit code (0 for success, 1 for failure).
    """
    stdout, stderr, returncode = ssh_run(config, '/opt/sat-agent/sat-agent status')

    if returncode != 0:
        print(f"{CROSS} Failed to get status: {stderr}")
        return 1

    try:
        response = json.loads(stdout)
    except json.JSONDecodeError:
        print(f"{CROSS} Invalid response from agent: {stdout}")
        return 1

    if response.get('status') != 'ok':
        print(f"{CROSS} Agent error: {response.get('reason', 'Unknown error')}")
        return 1

    services = response.get('services', {})

    for name, status in services.items():
        symbol = CHECK if status == 'running' else CROSS
        print(f"{symbol} {name}: {status}")

    return 0


def format_duration(seconds):
    """Format duration in human-readable format.

    Args:
        seconds: Duration in seconds.

    Returns:
        str: Formatted duration like "0.5s" or "1m 30s".
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m {secs}s"


def cmd_deploy(config, service, binary_path):
    """Execute the deploy command.

    Args:
        config: Configuration dictionary.
        service: Service name to deploy.
        binary_path: Path to local binary file.

    Returns:
        int: Exit code (0 for success, 1 for failure).
    """
    start_time = time.time()

    # Validate service exists
    services = config.get('services', {})
    if service not in services:
        print(f"{CROSS} Unknown service: {service}")
        return 1

    # Validate binary exists
    if not Path(binary_path).exists():
        print(f"{CROSS} Binary not found: {binary_path}")
        return 1

    # Upload binary
    print(f"{ARROW} Uploading {service}...")
    success, error = rsync_upload(config, binary_path, service)
    if not success:
        print(f"{CROSS} Upload failed: {error}")
        return 1

    # Run deploy on agent
    print(f"{ARROW} Deploying {service}...")
    stdout, stderr, returncode = ssh_run(
        config,
        f'/opt/sat-agent/sat-agent deploy {service}'
    )

    if returncode != 0:
        print(f"{CROSS} Deploy failed: {stderr}")
        return 1

    try:
        response = json.loads(stdout)
    except json.JSONDecodeError:
        print(f"{CROSS} Invalid response from agent: {stdout}")
        return 1

    if response.get('status') != 'ok':
        print(f"{CROSS} Deploy failed: {response.get('reason', 'Unknown error')}")
        return 1

    elapsed = time.time() - start_time
    file_hash = response.get('hash', 'unknown')
    print(f"{CHECK} Deployed {service} ({file_hash} in {format_duration(elapsed)})")

    return 0


def cmd_rollback(config, service):
    """Execute the rollback command.

    Args:
        config: Configuration dictionary.
        service: Service name to rollback.

    Returns:
        int: Exit code (0 for success, 1 for failure).
    """
    # Validate service exists
    services = config.get('services', {})
    if service not in services:
        print(f"{CROSS} Unknown service: {service}")
        return 1

    # Run rollback on agent
    print(f"{ARROW} Rolling back {service}...")
    stdout, stderr, returncode = ssh_run(
        config,
        f'/opt/sat-agent/sat-agent rollback {service}'
    )

    if returncode != 0:
        print(f"{CROSS} Rollback failed: {stderr}")
        return 1

    try:
        response = json.loads(stdout)
    except json.JSONDecodeError:
        print(f"{CROSS} Invalid response from agent: {stdout}")
        return 1

    if response.get('status') != 'ok':
        print(f"{CROSS} Rollback failed: {response.get('reason', 'Unknown error')}")
        return 1

    file_hash = response.get('hash', 'unknown')
    print(f"{CHECK} Rolled back {service} ({file_hash})")

    return 0


def cmd_restart(config, service):
    """Execute the restart command.

    Args:
        config: Configuration dictionary.
        service: Service name to restart.

    Returns:
        int: Exit code (0 for success, 1 for failure).
    """
    # Validate service exists
    services = config.get('services', {})
    if service not in services:
        print(f"{CROSS} Unknown service: {service}")
        return 1

    # Run restart on agent
    print(f"{ARROW} Restarting {service}...")
    stdout, stderr, returncode = ssh_run(
        config,
        f'/opt/sat-agent/sat-agent restart {service}'
    )

    if returncode != 0:
        print(f"{CROSS} Restart failed: {stderr}")
        return 1

    try:
        response = json.loads(stdout)
    except json.JSONDecodeError:
        print(f"{CROSS} Invalid response from agent: {stdout}")
        return 1

    if response.get('status') != 'ok':
        print(f"{CROSS} Restart failed: {response.get('reason', 'Unknown error')}")
        return 1

    print(f"{CHECK} Restarted {service}")

    return 0


def cmd_logs(config, service):
    """Execute the logs command (stream journalctl output).

    Args:
        config: Configuration dictionary.
        service: Service name to get logs for.

    Returns:
        int: Exit code (0 for success, 1 for failure).
    """
    # Validate service exists
    services = config.get('services', {})
    if service not in services:
        print(f"{CROSS} Unknown service: {service}")
        return 1

    service_config = services[service]
    systemd_name = service_config.get('systemd', f'{service}.service')

    flatsat = config.get('flatsat', {})
    host = flatsat.get('host', 'flatsat-disco.local')
    user = flatsat.get('user', 'root')

    # Stream logs directly (don't capture output)
    ssh_cmd = ['ssh', f'{user}@{host}', f'journalctl -f -u {systemd_name}']

    result = subprocess.run(ssh_cmd)

    return result.returncode


def print_usage():
    """Print usage information."""
    print(__doc__)


def main():
    """Main entry point for sat CLI."""
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    command = sys.argv[1]

    try:
        config = load_config()

        if command == 'status':
            sys.exit(cmd_status(config))

        elif command == 'deploy':
            if len(sys.argv) < 4:
                print(f"{CROSS} Usage: sat deploy <service> <binary>")
                sys.exit(1)
            service = sys.argv[2]
            binary = sys.argv[3]
            sys.exit(cmd_deploy(config, service, binary))

        elif command == 'rollback':
            if len(sys.argv) < 3:
                print(f"{CROSS} Usage: sat rollback <service>")
                sys.exit(1)
            service = sys.argv[2]
            sys.exit(cmd_rollback(config, service))

        elif command == 'restart':
            if len(sys.argv) < 3:
                print(f"{CROSS} Usage: sat restart <service>")
                sys.exit(1)
            service = sys.argv[2]
            sys.exit(cmd_restart(config, service))

        elif command == 'logs':
            if len(sys.argv) < 3:
                print(f"{CROSS} Usage: sat logs <service>")
                sys.exit(1)
            service = sys.argv[2]
            sys.exit(cmd_logs(config, service))

        else:
            print(f"{CROSS} Unknown command: {command}")
            print_usage()
            sys.exit(1)

    except FileNotFoundError as e:
        print(f"{CROSS} {e}")
        sys.exit(1)
    except Exception as e:
        print(f"{CROSS} Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
