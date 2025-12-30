"""SSH transport implementation."""

from typing import Optional

from satdeploy.deployer import Deployer
from satdeploy.services import ServiceManager, ServiceStatus
from satdeploy.ssh import SSHClient, SSHError
from satdeploy.transport.base import (
    Transport,
    TransportError,
    DeployResult,
    AppStatus,
    BackupInfo,
)


class SSHTransport(Transport):
    """Transport implementation using SSH/SFTP.

    This transport executes commands and transfers files via SSH,
    providing the same functionality as the original satdeploy
    implementation but through the Transport interface.
    """

    def __init__(
        self,
        host: str,
        user: str,
        backup_dir: str,
        port: int = 22,
        max_backups: int = 10,
    ):
        """Initialize SSH transport.

        Args:
            host: Target host address.
            user: SSH username.
            backup_dir: Remote directory for backups.
            port: SSH port (default 22).
            max_backups: Maximum backups to keep per app.
        """
        self.host = host
        self.user = user
        self.backup_dir = backup_dir
        self.port = port
        self.max_backups = max_backups
        self._ssh: Optional[SSHClient] = None
        self._deployer: Optional[Deployer] = None
        self._service_manager: Optional[ServiceManager] = None

    def connect(self) -> None:
        """Establish SSH connection.

        Raises:
            TransportError: If connection fails.
        """
        self._ssh = SSHClient(self.host, self.user, self.port)
        try:
            self._ssh.connect()
        except SSHError as e:
            raise TransportError(str(e)) from e

        self._deployer = Deployer(self._ssh, self.backup_dir, self.max_backups)
        self._service_manager = ServiceManager(self._ssh)

    def disconnect(self) -> None:
        """Close SSH connection."""
        if self._ssh:
            self._ssh.disconnect()
            self._ssh = None
        self._deployer = None
        self._service_manager = None

    def deploy(
        self,
        app_name: str,
        local_path: str,
        remote_path: str,
        param_name: Optional[str] = None,
        appsys_node: Optional[int] = None,
        run_node: Optional[int] = None,
        expected_checksum: Optional[str] = None,
        service_name: Optional[str] = None,
    ) -> DeployResult:
        """Deploy a binary via SSH/SFTP.

        Args:
            app_name: Name of the application.
            local_path: Path to the local binary.
            remote_path: Path on the target where binary should be installed.
            param_name: Ignored for SSH transport.
            appsys_node: Ignored for SSH transport.
            run_node: Ignored for SSH transport.
            expected_checksum: Optional checksum to verify after upload.
            service_name: Optional systemd service to stop/start.

        Returns:
            DeployResult indicating success/failure and backup path.
        """
        if not self._deployer or not self._service_manager:
            return DeployResult(
                success=False,
                error_message="Not connected"
            )

        try:
            # Stop service if specified
            if service_name:
                self._service_manager.stop(service_name)

            # Backup existing binary
            backup_path = self._deployer.backup(app_name, remote_path)

            # Deploy new binary
            self._deployer.deploy(local_path, remote_path)

            # Verify checksum if provided
            if expected_checksum:
                actual = self._deployer.compute_remote_hash(remote_path)
                if actual != expected_checksum:
                    return DeployResult(
                        success=False,
                        error_message=f"Checksum mismatch: expected {expected_checksum}, got {actual}",
                        backup_path=backup_path,
                    )

            # Start service if specified
            if service_name:
                self._service_manager.start(service_name)

            return DeployResult(
                success=True,
                backup_path=backup_path,
            )

        except SSHError as e:
            return DeployResult(
                success=False,
                error_message=str(e),
            )

    def rollback(
        self,
        app_name: str,
        backup_hash: Optional[str] = None,
        remote_path: Optional[str] = None,
        service_name: Optional[str] = None,
    ) -> DeployResult:
        """Rollback to a previous version via SSH.

        Args:
            app_name: Name of the application.
            backup_hash: Specific backup hash to restore, or None for latest.
            remote_path: Path to restore to on target.
            service_name: Optional systemd service to stop/start.

        Returns:
            DeployResult indicating success/failure.
        """
        if not self._deployer or not self._service_manager:
            return DeployResult(
                success=False,
                error_message="Not connected"
            )

        try:
            # Get list of backups
            backups = self._deployer.list_backups(app_name)
            if not backups:
                return DeployResult(
                    success=False,
                    error_message=f"No backups found for {app_name}"
                )

            # Find the backup to restore
            if backup_hash:
                backup = next(
                    (b for b in backups if b.get("hash") == backup_hash),
                    None
                )
                if not backup:
                    return DeployResult(
                        success=False,
                        error_message=f"No backup with hash {backup_hash} found"
                    )
            else:
                # Use the most recent backup
                backup = backups[0]

            # Stop service if specified
            if service_name:
                self._service_manager.stop(service_name)

            # Restore the backup
            if remote_path:
                self._deployer.restore(backup["path"], remote_path)

            # Start service if specified
            if service_name:
                self._service_manager.start(service_name)

            return DeployResult(success=True)

        except SSHError as e:
            return DeployResult(
                success=False,
                error_message=str(e),
            )

    def get_status(self, apps: dict[str, dict]) -> dict[str, AppStatus]:
        """Get status of deployed applications via SSH.

        Args:
            apps: Dictionary of app configs with 'remote' and 'service' keys.

        Returns:
            Dictionary mapping app names to their status.
        """
        if not self._deployer or not self._service_manager or not self._ssh:
            return {}

        result = {}
        for app_name, config in apps.items():
            remote_path = config.get("remote", "")
            service = config.get("service")

            # Check if binary exists and get its hash
            binary_hash = None
            if self._ssh.file_exists(remote_path):
                binary_hash = self._deployer.compute_remote_hash(remote_path)

            # Check if service is running
            running = False
            if service:
                status = self._service_manager.get_status(service)
                running = status == ServiceStatus.RUNNING

            result[app_name] = AppStatus(
                app_name=app_name,
                running=running,
                binary_hash=binary_hash,
                remote_path=remote_path,
            )

        return result

    def list_backups(self, app_name: str) -> list[BackupInfo]:
        """List available backups via SSH.

        Args:
            app_name: Name of the application.

        Returns:
            List of BackupInfo, sorted newest first.
        """
        if not self._deployer:
            return []

        backups = self._deployer.list_backups(app_name)
        return [
            BackupInfo(
                version=b["version"],
                timestamp=b["timestamp"],
                binary_hash=b.get("hash"),
                path=b["path"],
            )
            for b in backups
        ]

    def verify(self, app_name: str, remote_path: str) -> Optional[str]:
        """Verify the installed binary checksum via SSH.

        Args:
            app_name: Name of the application.
            remote_path: Path to the binary on target.

        Returns:
            The checksum (first 8 chars of SHA256), or None if not found.
        """
        if not self._deployer:
            return None

        return self._deployer.compute_remote_hash(remote_path)
