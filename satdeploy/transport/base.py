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
    file_hash: Optional[str] = None
    skipped: bool = False
    restored: bool = False


@dataclass
class AppStatus:
    """Status of a deployed application."""

    app_name: str
    running: bool
    file_hash: Optional[str]
    remote_path: str


@dataclass
class BackupInfo:
    """Information about a backup."""

    version: str
    timestamp: str
    file_hash: Optional[str]
    path: str


class Transport(ABC):
    """Abstract base class for transport implementations.

    Transport implementations provide communication between satdeploy
    and the target device. Each transport handles:
    - Connection management (connect/disconnect)
    - Deployment operations (deploy file, rollback)
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
        services: Optional[list[tuple[str, str]]] = None,
        force: bool = False,
    ) -> DeployResult:
        """Deploy a file to the target.

        Args:
            app_name: Name of the application.
            local_path: Path to the local file.
            remote_path: Path on the target where file should be installed.
            param_name: For CSP: the libparam parameter name (e.g., "mng_dipp").
            appsys_node: For CSP: the app-sys-manager CSP node address.
            run_node: For CSP: the CSP node address where app runs.
            expected_checksum: Expected SHA256 checksum (first 8 chars).
            services: For SSH: list of (app_name, service_name) tuples to
                stop/start in dependency order.

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
    def get_logs(self, app_name: str, service: str, lines: int = 100) -> Optional[str]:
        """Fetch service logs from the target.

        Args:
            app_name: Name of the application.
            service: Service name (e.g., controller.service).
            lines: Number of log lines to fetch.

        Returns:
            Log output string, or None on failure.
        """
        pass

    def exec_command(
        self,
        command: str,
        timeout: Optional[float] = None,
    ) -> tuple[int, str, str]:
        """Run a shell command on the target and capture exit code + output.

        Used by `satdeploy validate` to invoke the per-app
        ``validate_command`` in the target's runtime context. The string is
        interpreted by the target's default shell — callers are responsible
        for quoting.

        Default implementation raises NotImplementedError. Concrete
        transports that can spawn target-side processes (SSH, Local)
        override this. CSP cannot — Python doesn't speak CSP per the
        2026-04-17 architectural boundary, and CSP-side validate is on the
        Phase-1 roadmap inside satdeploy-apm (C).

        Args:
            command: Shell command string.
            timeout: Hard wall-clock timeout in seconds, or None for no
                timeout. On timeout, raises TransportError.

        Returns:
            (exit_code, stdout, stderr) — exit_code 0 means PASS by
            convention; nonzero means FAIL.

        Raises:
            TransportError: If the command cannot be launched, the
                connection drops mid-run, or the timeout fires.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support exec_command"
        )

    def __enter__(self) -> "Transport":
        """Enter context manager."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit context manager."""
        self.disconnect()
