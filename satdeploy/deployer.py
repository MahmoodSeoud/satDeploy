"""Deployment logic for satdeploy."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from satdeploy.services import ServiceManager
    from satdeploy.ssh import SSHClient


def parse_backup_version(version: str) -> dict:
    """Parse version string to extract timestamp and hash.

    Supports both old format (YYYYMMDD-HHMMSS) and new format (YYYYMMDD-HHMMSS-hash).
    """
    parts = version.split("-")
    if len(parts) >= 3:
        # New format: YYYYMMDD-HHMMSS-hash
        timestamp_str = f"{parts[0]}-{parts[1]}"
        binary_hash = parts[2] if len(parts) > 2 else None
    else:
        # Old format: YYYYMMDD-HHMMSS
        timestamp_str = version
        binary_hash = None

    try:
        dt = datetime.strptime(timestamp_str, "%Y%m%d-%H%M%S")
        timestamp = dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        timestamp = version

    return {"timestamp": timestamp, "hash": binary_hash}


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

    def compute_remote_hash(self, remote_path: str) -> Optional[str]:
        """Compute SHA256 hash of a remote file.

        Args:
            remote_path: Path to the remote file.

        Returns:
            First 8 characters of the hex digest, or None if file doesn't exist.
        """
        result = self._ssh.run(f"sha256sum '{remote_path}' 2>/dev/null", check=False)
        if result.exit_code != 0 or not result.stdout.strip():
            return None
        # sha256sum output: "hash  filename"
        return result.stdout.strip().split()[0][:8]

    def list_backups(self, app_name: str) -> list[dict]:
        """List available backups for an app.

        Args:
            app_name: The application name.

        Returns:
            List of backup info dicts with keys: version, timestamp, hash, path.
            Sorted by version (newest first).
        """
        backup_dir = f"{self._backup_dir}/{app_name}"
        result = self._ssh.run(f"ls '{backup_dir}' 2>/dev/null || true", check=False)

        backups = []
        for line in result.stdout.strip().split("\n"):
            if not line or not line.endswith(".bak"):
                continue
            version = line.replace(".bak", "")
            parsed = parse_backup_version(version)
            backups.append({
                "version": version,
                "timestamp": parsed["timestamp"],
                "hash": parsed["hash"],
                "path": f"{backup_dir}/{line}",
            })

        backups.sort(key=lambda b: b["version"], reverse=True)
        return backups

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

        # Compute hash of file being backed up
        file_hash = self.compute_remote_hash(remote_path)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        if file_hash:
            backup_path = f"{backup_dir}/{timestamp}-{file_hash}.bak"
        else:
            backup_path = f"{backup_dir}/{timestamp}.bak"

        self._ssh.run(f"cp '{remote_path}' '{backup_path}'")

        return backup_path

    def deploy(self, local_path: str, remote_path: str) -> None:
        """Deploy a local binary to the remote path.

        Args:
            local_path: Path to the local binary.
            remote_path: Path on the remote host.
        """
        # Create parent directory if it doesn't exist
        parent_dir = "/".join(remote_path.rsplit("/", 1)[:-1])
        if parent_dir:
            self._ssh.run(f"mkdir -p '{parent_dir}'")

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

    def rollback(
        self,
        app_name: str,
        remote_path: str,
        service: Optional[str],
        service_manager: "ServiceManager",
        version: Optional[str] = None,
    ) -> DeployResult:
        """Rollback to a previous version.

        Args:
            app_name: The application name.
            remote_path: Path on the remote host.
            service: The systemd service name, or None for libraries.
            service_manager: The service manager instance.
            version: Specific version to restore, or None for most recent.

        Returns:
            DeployResult with success status and metadata.
        """
        try:
            backups = self.list_backups(app_name)

            if not backups:
                return DeployResult(
                    success=False,
                    app_name=app_name,
                    error_message="No backups available for rollback",
                )

            if version:
                matching = [b for b in backups if b["version"] == version]
                if not matching:
                    return DeployResult(
                        success=False,
                        app_name=app_name,
                        error_message=f"Version {version} not found",
                    )
                backup = matching[0]
            else:
                backup = backups[0]

            backup_path = backup["path"]

            if service:
                service_manager.stop(service)

            self._ssh.run(f"cp '{backup_path}' '{remote_path}'")
            self._ssh.run(f"chmod +x '{remote_path}'")

            health_check_passed = None
            if service:
                service_manager.start(service)
                health_check_passed = service_manager.is_healthy(service)

            return DeployResult(
                success=True,
                app_name=app_name,
                backup_path=backup_path,
                health_check_passed=health_check_passed,
            )

        except Exception as e:
            return DeployResult(
                success=False,
                app_name=app_name,
                error_message=str(e),
            )
