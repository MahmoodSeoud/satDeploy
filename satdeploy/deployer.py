"""Deployment logic for satdeploy."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from satdeploy.services import ServiceManager
    from satdeploy.ssh import SSHClient


@dataclass
class DeployResult:
    """Result of a deployment operation."""

    success: bool
    app_name: str
    binary_hash: Optional[str] = None
    backup_path: Optional[str] = None
    health_check_passed: Optional[bool] = None
    error_message: Optional[str] = None


class Deployer:
    """Handles deployment of binaries to remote target."""

    def __init__(
        self,
        ssh: "SSHClient",
        backup_dir: str,
        max_backups: int = 10,
    ):
        self._ssh = ssh
        self._backup_dir = backup_dir
        self._max_backups = max_backups

    def compute_hash(self, local_path: str) -> str:
        """Compute SHA256 hash of a local file.

        Args:
            local_path: Path to the local file.

        Returns:
            First 8 characters of the hex digest.
        """
        sha256 = hashlib.sha256()
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()[:8]

    def backup(self, app_name: str, remote_path: str) -> Optional[str]:
        """Create a backup of the current remote binary.

        Args:
            app_name: The application name.
            remote_path: Path to the remote binary.

        Returns:
            The backup path, or None if no backup was created.
        """
        if not self._ssh.file_exists(remote_path):
            return None

        backup_dir = f"{self._backup_dir}/{app_name}"
        self._ssh.run(f"mkdir -p '{backup_dir}'")

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = f"{backup_dir}/{timestamp}.bak"

        self._ssh.run(f"cp '{remote_path}' '{backup_path}'")

        return backup_path

    def deploy(self, local_path: str, remote_path: str) -> None:
        """Deploy a local binary to the remote path.

        Args:
            local_path: Path to the local binary.
            remote_path: Path on the remote host.
        """
        self._ssh.upload(local_path, remote_path)
        self._ssh.run(f"chmod +x '{remote_path}'")

    def push(
        self,
        app_name: str,
        local_path: str,
        remote_path: str,
        service: Optional[str],
        service_manager: "ServiceManager",
    ) -> DeployResult:
        """Push a binary to the target.

        This performs the full deployment workflow:
        1. Compute hash of local binary
        2. Backup current remote binary
        3. Stop service (if applicable)
        4. Deploy new binary
        5. Start service (if applicable)
        6. Health check (if applicable)

        Args:
            app_name: The application name.
            local_path: Path to the local binary.
            remote_path: Path on the remote host.
            service: The systemd service name, or None for libraries.
            service_manager: The service manager instance.

        Returns:
            DeployResult with success status and metadata.
        """
        try:
            binary_hash = self.compute_hash(local_path)
            backup_path = self.backup(app_name, remote_path)

            if service:
                service_manager.stop(service)

            self.deploy(local_path, remote_path)

            health_check_passed = None
            if service:
                service_manager.start(service)
                health_check_passed = service_manager.is_healthy(service)

            return DeployResult(
                success=True,
                app_name=app_name,
                binary_hash=binary_hash,
                backup_path=backup_path,
                health_check_passed=health_check_passed,
            )

        except Exception as e:
            return DeployResult(
                success=False,
                app_name=app_name,
                error_message=str(e),
            )
