#!/usr/bin/env python3
"""sat-agent: Deployment agent for flatsat.

Runs on the flatsat to handle deployment commands from the CLI.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
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


def stop_service(service_name):
    """Stop a systemd service.

    Args:
        service_name: Name of the systemd service.
    """
    subprocess.run(
        ['systemctl', 'stop', service_name],
        capture_output=True,
        text=True,
        check=True
    )


def start_service(service_name):
    """Start a systemd service.

    Args:
        service_name: Name of the systemd service.
    """
    subprocess.run(
        ['systemctl', 'start', service_name],
        capture_output=True,
        text=True,
        check=True
    )


def backup_binary(service, config):
    """Backup current binary before deployment.

    Args:
        service: Service name.
        config: Configuration dictionary.
    """
    services = config.get('services', {})
    service_config = services.get(service, {})
    binary_path = Path(service_config.get('binary', ''))
    backup_dir = Path(config.get('backup_dir', '/opt/sat-agent/backups'))

    if not binary_path.exists():
        return  # First deploy, nothing to backup

    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f'{service}.prev'

    shutil.copy2(binary_path, backup_path)


def swap_binary(service, config):
    """Swap .new binary to final path.

    Args:
        service: Service name.
        config: Configuration dictionary.

    Raises:
        FileNotFoundError: If .new file doesn't exist.
    """
    services = config.get('services', {})
    service_config = services.get(service, {})
    binary_path = Path(service_config.get('binary', ''))
    new_path = Path(str(binary_path) + '.new')

    if not new_path.exists():
        raise FileNotFoundError(f"New binary not found: {new_path}")

    new_path.rename(binary_path)
    binary_path.chmod(0o755)


def compute_hash(file_path):
    """Compute SHA256 hash of file (first 8 chars).

    Args:
        file_path: Path to the file.

    Returns:
        str: First 8 characters of hex digest.
    """
    with open(file_path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()[:8]


def log_deployment(service, file_hash, config):
    """Log deployment to versions.json.

    Args:
        service: Service name.
        file_hash: Hash of deployed binary.
        config: Configuration dictionary.
    """
    version_log = Path(config.get('version_log', '/opt/sat-agent/versions.json'))

    entries = []
    if version_log.exists():
        entries = json.loads(version_log.read_text())

    entries.append({
        'service': service,
        'hash': file_hash,
        'timestamp': datetime.now().isoformat()
    })

    version_log.parent.mkdir(parents=True, exist_ok=True)
    version_log.write_text(json.dumps(entries, indent=2))


def restore_binary(service, config):
    """Restore backup binary to original path.

    Args:
        service: Service name.
        config: Configuration dictionary.

    Raises:
        FileNotFoundError: If backup file doesn't exist.
    """
    services = config.get('services', {})
    service_config = services.get(service, {})
    binary_path = Path(service_config.get('binary', ''))
    backup_dir = Path(config.get('backup_dir', '/opt/sat-agent/backups'))
    backup_path = backup_dir / f'{service}.prev'

    if not backup_path.exists():
        raise FileNotFoundError(f"No backup found: {backup_path}")

    shutil.copy2(backup_path, binary_path)
    binary_path.chmod(0o755)


def rollback(service, config):
    """Rollback a service to its previous version.

    Args:
        service: Service name to rollback.
        config: Configuration dictionary.

    Returns:
        dict: Result with 'status', 'service', and 'hash' or 'reason'.
    """
    services = config.get('services', {})

    if service not in services:
        return {'status': 'failed', 'reason': f'Unknown service: {service}'}

    service_config = services[service]
    systemd_name = service_config.get('systemd', f'{service}.service')

    try:
        # Stop services in order (dependents first)
        stop_order = get_stop_order(service, config)
        for svc in stop_order:
            svc_config = services.get(svc, {})
            svc_systemd = svc_config.get('systemd', f'{svc}.service')
            stop_service(svc_systemd)

        # Restore backup binary
        restore_binary(service, config)

        # Compute hash of restored binary
        binary_path = Path(service_config.get('binary', ''))
        file_hash = compute_hash(binary_path)

        # Start services in order (service first, then dependents)
        start_order = get_start_order(service, config)
        for svc in start_order:
            svc_config = services.get(svc, {})
            svc_systemd = svc_config.get('systemd', f'{svc}.service')
            start_service(svc_systemd)

        # Verify service is running
        if check_service_status(systemd_name) != 'running':
            return {
                'status': 'failed',
                'reason': f'Service {service} not running after rollback'
            }

        return {'status': 'ok', 'service': service, 'hash': file_hash}

    except Exception as e:
        return {'status': 'failed', 'reason': str(e)}


def deploy(service, config):
    """Deploy a service.

    Args:
        service: Service name to deploy.
        config: Configuration dictionary.

    Returns:
        dict: Result with 'status', 'service', and 'hash' or 'reason'.
    """
    services = config.get('services', {})

    if service not in services:
        return {'status': 'failed', 'reason': f'Unknown service: {service}'}

    service_config = services[service]
    systemd_name = service_config.get('systemd', f'{service}.service')

    try:
        # Stop services in order (dependents first)
        stop_order = get_stop_order(service, config)
        for svc in stop_order:
            svc_config = services.get(svc, {})
            svc_systemd = svc_config.get('systemd', f'{svc}.service')
            stop_service(svc_systemd)

        # Backup and swap binary
        backup_binary(service, config)
        swap_binary(service, config)

        # Compute hash of new binary
        binary_path = Path(service_config.get('binary', ''))
        file_hash = compute_hash(binary_path)

        # Start services in order (service first, then dependents)
        start_order = get_start_order(service, config)
        for svc in start_order:
            svc_config = services.get(svc, {})
            svc_systemd = svc_config.get('systemd', f'{svc}.service')
            start_service(svc_systemd)

        # Verify service is running
        if check_service_status(systemd_name) != 'running':
            return {
                'status': 'failed',
                'reason': f'Service {service} not running after start'
            }

        # Log deployment
        log_deployment(service, file_hash, config)

        return {'status': 'ok', 'service': service, 'hash': file_hash}

    except Exception as e:
        return {'status': 'failed', 'reason': str(e)}


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
        elif command == 'deploy':
            if len(sys.argv) < 3:
                print(json.dumps({'status': 'failed', 'reason': 'deploy requires service name'}))
                sys.exit(1)
            service = sys.argv[2]
            result = deploy(service, config)
            print(json.dumps(result))
            if result['status'] == 'failed':
                sys.exit(1)
        elif command == 'rollback':
            if len(sys.argv) < 3:
                print(json.dumps({'status': 'failed', 'reason': 'rollback requires service name'}))
                sys.exit(1)
            service = sys.argv[2]
            result = rollback(service, config)
            print(json.dumps(result))
            if result['status'] == 'failed':
                sys.exit(1)
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
