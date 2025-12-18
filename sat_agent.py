#!/usr/bin/env python3
"""sat-agent: Deployment agent for flatsat.

Runs on the flatsat to handle deployment commands from the CLI.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml


DEFAULT_CONFIG_PATH = '/opt/sat-agent/config.yaml'


def load_config(config_path=None):
    """Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, uses SAT_AGENT_CONFIG
                     env var or falls back to DEFAULT_CONFIG_PATH.

    Returns:
        dict: Configuration dictionary.

    Raises:
        FileNotFoundError: If config file doesn't exist.
    """
    if config_path is None:
        config_path = os.environ.get('SAT_AGENT_CONFIG', DEFAULT_CONFIG_PATH)

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(path) as f:
        return yaml.safe_load(f)


def check_service_status(service_name):
    """Check if a systemd service is running.

    Args:
        service_name: Name of the systemd service (e.g., 'controller.service').

    Returns:
        str: 'running' if active, 'stopped' otherwise.
    """
    result = subprocess.run(
        ['systemctl', 'is-active', service_name],
        capture_output=True,
        text=True
    )
    return 'running' if result.returncode == 0 else 'stopped'


def get_dependents(service, config):
    """Find all services that depend on the given service (transitively).

    Args:
        service: Name of the service to find dependents for.
        config: Configuration dictionary with 'services' key.

    Returns:
        list: Services that depend on this one, ordered for stopping
              (direct dependents first, then their dependents).
    """
    services = config.get('services', {})
    dependents = []
    visited = set()

    def find_direct_dependents(svc):
        """Find services that directly depend on svc."""
        direct = []
        for name, svc_config in services.items():
            deps = svc_config.get('depends_on', [])
            if svc in deps and name not in visited:
                direct.append(name)
        return direct

    def collect_dependents(svc):
        """Recursively collect all dependents."""
        direct = find_direct_dependents(svc)
        for dep in direct:
            if dep not in visited:
                visited.add(dep)
                dependents.append(dep)
                collect_dependents(dep)

    collect_dependents(service)
    return dependents


def get_stop_order(service, config):
    """Get order to stop services for deployment (top-down).

    Stops dependents first, then the service itself.

    Args:
        service: Name of the service being deployed.
        config: Configuration dictionary.

    Returns:
        list: Services to stop, in order.
    """
    dependents = get_dependents(service, config)
    return dependents + [service]


def get_start_order(service, config):
    """Get order to start services after deployment (bottom-up).

    Starts the service first, then its dependents.

    Args:
        service: Name of the service being deployed.
        config: Configuration dictionary.

    Returns:
        list: Services to start, in order.
    """
    return list(reversed(get_stop_order(service, config)))


def get_status(config):
    """Get status of all configured services.

    Args:
        config: Configuration dictionary with 'services' key.

    Returns:
        dict: Status response with 'status' and 'services' keys.
    """
    services = config.get('services', {})
    service_statuses = {}

    for name, service_config in services.items():
        systemd_name = service_config.get('systemd', f'{name}.service')
        service_statuses[name] = check_service_status(systemd_name)

    return {
        'status': 'ok',
        'services': service_statuses
    }


def main():
    """Main entry point for sat-agent CLI."""
    if len(sys.argv) < 2:
        print(json.dumps({'status': 'failed', 'reason': 'No command provided'}))
        sys.exit(1)

    command = sys.argv[1]

    try:
        config = load_config()

        if command == 'status':
            result = get_status(config)
            print(json.dumps(result))
        else:
            print(json.dumps({'status': 'failed', 'reason': f'Unknown command: {command}'}))
            sys.exit(1)

    except FileNotFoundError as e:
        print(json.dumps({'status': 'failed', 'reason': str(e)}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({'status': 'failed', 'reason': str(e)}))
        sys.exit(1)


if __name__ == '__main__':
    main()
