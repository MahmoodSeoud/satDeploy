"""Abstract base class for transport implementations."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


class TransportError(Exception):
    """Exception raised for transport connection and operation failures."""

    pass


@dataclass
class DeployResult:
    """Result of a deployment operation."""

    success: bool
    backup_path: Optional[str] = None
    error_code: Optional[int] = None
    error_message: Optional[str] = None


@dataclass
class AppStatus:
    """Status of a deployed application."""

    app_name: str
    running: bool
    binary_hash: Optional[str]
    remote_path: str


@dataclass
class BackupInfo:
    """Information about a backup."""

    version: str
    timestamp: str
    binary_hash: Optional[str]
    path: str


class Transport(ABC):
    """Abstract base class for transport implementations.

    Transport implementations provide communication between satdeploy
    and the target device. Each transport handles:
    - Connection management (connect/disconnect)
    - Deployment operations (deploy binary, rollback)
    - Status queries (app status, backup listing)
    - Verification (checksum validation)

    The SSH transport implements these by executing commands remotely.
    The CSP transport implements these by sending protocol messages
    to a satellite agent.
    """

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the target.

        Raises:
            TransportError: If connection fails.
        """
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection to the target."""
        pass

    @abstractmethod
    def deploy(
        self,
        app_name: str,
        local_path: str,
        remote_path: str,
        param_name: Optional[str] = None,
        appsys_node: Optional[int] = None,
        run_node: Optional[int] = None,
        expected_checksum: Optional[str] = None,
    ) -> DeployResult:
        """Deploy a binary to the target.

        Args:
            app_name: Name of the application.
            local_path: Path to the local binary.
            remote_path: Path on the target where binary should be installed.
            param_name: For CSP: the libparam parameter name (e.g., "mng_dipp").
            appsys_node: For CSP: the app-sys-manager CSP node address.
            run_node: For CSP: the CSP node address where app runs.
            expected_checksum: Expected SHA256 checksum (first 8 chars).

        Returns:
            DeployResult indicating success/failure and backup path.
        """
        pass

    @abstractmethod
    def rollback(
        self,
        app_name: str,
        backup_hash: Optional[str] = None,
    ) -> DeployResult:
        """Rollback to a previous version.

        Args:
            app_name: Name of the application.
            backup_hash: Specific backup hash to restore, or None for latest.

        Returns:
            DeployResult indicating success/failure.
        """
        pass

    @abstractmethod
    def get_status(self) -> dict[str, AppStatus]:
        """Get status of all deployed applications.

        Returns:
            Dictionary mapping app names to their status.
        """
        pass

    @abstractmethod
    def list_backups(self, app_name: str) -> list[BackupInfo]:
        """List available backups for an application.

        Args:
            app_name: Name of the application.

        Returns:
            List of BackupInfo, sorted newest first.
        """
        pass

    @abstractmethod
    def verify(self, app_name: str, remote_path: str) -> Optional[str]:
        """Verify the installed binary checksum.

        Args:
            app_name: Name of the application.
            remote_path: Path to the binary on target.

        Returns:
            The checksum (first 8 chars of SHA256), or None if not found.
        """
        pass

    def __enter__(self) -> "Transport":
        """Enter context manager."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context manager."""
        self.disconnect()
