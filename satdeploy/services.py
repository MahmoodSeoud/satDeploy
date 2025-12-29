"""Systemd service management for satdeploy."""

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from satdeploy.ssh import SSHClient


class ServiceStatus(Enum):
    """Status of a systemd service."""

    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ServiceManager:
    """Manages systemd services via SSH."""

    def __init__(self, ssh: "SSHClient"):
        self._ssh = ssh

    def get_status(self, service: str) -> ServiceStatus:
        """Get the status of a service.

        Args:
            service: The service name (e.g., "controller.service").

        Returns:
            The service status.
        """
        result = self._ssh.run(f"sudo systemctl is-active {service}", check=False)
        status_text = result.stdout.strip()

        if status_text == "active":
            return ServiceStatus.RUNNING
        elif status_text == "inactive":
            return ServiceStatus.STOPPED
        elif status_text == "failed":
            return ServiceStatus.FAILED
        else:
            return ServiceStatus.UNKNOWN

    def stop(self, service: str) -> bool:
        """Stop a service.

        Args:
            service: The service name.

        Returns:
            True if service was stopped, False if service doesn't exist.

        Raises:
            SSHError: If stop fails for reasons other than service not existing.
        """
        result = self._ssh.run(f"sudo systemctl stop {service}", check=False)
        output = result.stdout + result.stderr

        if result.exit_code != 0:
            # Service doesn't exist - return False so caller can warn
            if "not loaded" in output.lower() or "not found" in output.lower():
                return False
            from satdeploy.ssh import SSHError
            raise SSHError(f"Failed to stop {service}: {output}")
        return True

    def start(self, service: str) -> bool:
        """Start a service.

        Args:
            service: The service name.

        Returns:
            True if service was started, False if service doesn't exist.

        Raises:
            SSHError: If start fails for reasons other than service not existing.
        """
        result = self._ssh.run(f"sudo systemctl start {service}", check=False)
        output = result.stdout + result.stderr

        if result.exit_code != 0:
            # Service doesn't exist - return False so caller can warn
            if "not loaded" in output.lower() or "not found" in output.lower():
                return False
            from satdeploy.ssh import SSHError
            raise SSHError(f"Failed to start {service}: {output}")
        return True

    def restart(self, service: str) -> None:
        """Restart a service.

        Args:
            service: The service name.
        """
        self._ssh.run(f"sudo systemctl restart {service}")

    def is_healthy(self, service: str) -> bool:
        """Check if a service is healthy (running).

        Args:
            service: The service name.

        Returns:
            True if the service is running, False otherwise.
        """
        return self.get_status(service) == ServiceStatus.RUNNING

    def get_logs(self, service: str, lines: int = 100) -> str:
        """Get recent logs for a service.

        Args:
            service: The service name.
            lines: Number of lines to retrieve.

        Returns:
            The log output.
        """
        result = self._ssh.run(
            f"sudo journalctl -u {service} -n {lines} --no-pager",
            check=False,
        )
        return result.stdout

    def daemon_reload(self) -> None:
        """Reload systemd daemon configuration.

        Call this after updating service files in /etc/systemd/system/.
        """
        self._ssh.run("sudo systemctl daemon-reload")

    def enable(self, service: str) -> None:
        """Enable a service to start on boot.

        Args:
            service: The service name.
        """
        self._ssh.run(f"sudo systemctl enable {service}", check=False)
