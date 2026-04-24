"""Local filesystem transport.

Deploys files to a directory on the local machine. Useful for:
- `satdeploy demo` (zero-prerequisite workflow demo)
- Deploying to a chroot, mounted rootfs, or NFS target
- Testing the deploy/rollback/history workflow without any target hardware

The "remote" is just a local directory. Everything else (hash verification,
versioned backups, rollback semantics, history recording) is identical to the
SSH and CSP transports — this is real product code, not a demo stub.
"""

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from satdeploy.hash import compute_file_hash
from satdeploy.paths import expand_path
from satdeploy.transport.base import (
    AppStatus,
    BackupInfo,
    DeployResult,
    Transport,
    TransportError,
)


# Backup filename: {YYYYMMDD}-{HHMMSS}-{hash8}.bak
_BACKUP_RE = re.compile(
    r"^(?P<date>\d{8})-(?P<time>\d{6})-(?P<hash>[0-9a-f]{8})\.bak$"
)


class LocalTransport(Transport):
    """Transport that deploys to a local directory.

    Layout:
        {target_dir}/                  — where deployed files live
        {backup_dir}/{app_name}/       — where versioned backups live
    """

    def __init__(
        self,
        target_dir: str,
        backup_dir: str,
        max_backups: int = 10,
        apps: Optional[dict[str, dict]] = None,
    ):
        self.target_dir = expand_path(target_dir)
        self.backup_dir = expand_path(backup_dir)
        self.max_backups = max_backups
        self._apps = apps or {}

    def connect(self) -> None:
        try:
            Path(self.target_dir).mkdir(parents=True, exist_ok=True)
            Path(self.backup_dir).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise TransportError(f"Cannot create local target/backup dirs: {e}") from e

    def disconnect(self) -> None:
        pass

    def _resolve_remote(self, remote_path: str) -> str:
        """Anchor a config 'remote' path under target_dir if absolute.

        A config like `remote: /opt/demo/bin/test_app` is conceptually an
        absolute path on the target. For the local transport, we treat
        target_dir as the target's root, so `/opt/demo/bin/test_app` lands
        at `{target_dir}/opt/demo/bin/test_app`. Relative paths are joined
        directly.
        """
        if os.path.isabs(remote_path):
            return os.path.join(self.target_dir, remote_path.lstrip("/"))
        return os.path.join(self.target_dir, remote_path)

    def _app_backup_dir(self, app_name: str) -> Path:
        return Path(self.backup_dir) / app_name

    def _prune_backups(self, app_name: str) -> None:
        backups = self.list_backups(app_name)
        for old in backups[self.max_backups:]:
            try:
                Path(old.path).unlink()
            except OSError:
                pass

    def _make_backup(self, app_name: str, resolved_remote: str) -> Optional[str]:
        """Copy the file currently at resolved_remote into the backup dir.

        Returns the backup path, or None if there was nothing to back up.
        Deduplicates: if the file we'd back up has the same hash as the
        most recent existing backup, returns the existing backup path
        without creating a new file. This prevents `push --force` of an
        identical binary from polluting the backup chain with redundant
        entries (which would otherwise make rollback no-op by pointing
        at the same hash that's already deployed).
        """
        if not os.path.exists(resolved_remote):
            return None

        file_hash = compute_file_hash(resolved_remote)

        existing = self.list_backups(app_name)
        if existing and existing[0].file_hash == file_hash:
            return existing[0].path

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_name = f"{ts}-{file_hash}.bak"
        app_dir = self._app_backup_dir(app_name)
        app_dir.mkdir(parents=True, exist_ok=True)
        backup_path = app_dir / backup_name

        shutil.copy2(resolved_remote, backup_path)
        return str(backup_path)

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
        try:
            local_hash = compute_file_hash(local_path)
            resolved_remote = self._resolve_remote(remote_path)

            current_hash: Optional[str] = None
            if os.path.exists(resolved_remote):
                current_hash = compute_file_hash(resolved_remote)

            # Hash-skip: same file already deployed (unless --force)
            if not force and current_hash == local_hash:
                return DeployResult(
                    success=True,
                    file_hash=local_hash,
                    skipped=True,
                )

            # Back up whatever is currently deployed before overwriting —
            # but NOT if the current file is byte-identical to what we're
            # about to write (e.g. `push --force` of an unchanged binary).
            # Backing up an identical copy just pollutes the backup chain
            # and makes subsequent `rollback` a no-op.
            if current_hash is not None and current_hash != local_hash:
                backup_path = self._make_backup(app_name, resolved_remote)
            else:
                backup_path = None

            # Copy the new file into place, preserving mode
            Path(resolved_remote).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, resolved_remote)

            # Trim old backups
            self._prune_backups(app_name)

            return DeployResult(
                success=True,
                backup_path=backup_path,
                file_hash=local_hash,
            )

        except OSError as e:
            return DeployResult(
                success=False,
                error_message=f"Local deploy failed: {e}",
            )

    def rollback(
        self,
        app_name: str,
        backup_hash: Optional[str] = None,
    ) -> DeployResult:
        backups = self.list_backups(app_name)
        if not backups:
            return DeployResult(
                success=False,
                error_message=f"No backups found for {app_name}",
            )

        if backup_hash:
            target = next(
                (b for b in backups if b.file_hash == backup_hash),
                None,
            )
            if target is None:
                return DeployResult(
                    success=False,
                    error_message=f"No backup with hash {backup_hash} found",
                )
        else:
            target = backups[0]

        # Figure out where to restore to — from the app config
        app_cfg = self._apps.get(app_name, {})
        remote_path = app_cfg.get("remote")
        if not remote_path:
            return DeployResult(
                success=False,
                error_message=(
                    f"No remote path configured for {app_name} — "
                    "cannot rollback"
                ),
            )

        resolved_remote = self._resolve_remote(remote_path)

        try:
            # Back up the current file before overwriting it with the old one,
            # so the user can roll forward again if they change their mind.
            self._make_backup(app_name, resolved_remote)

            Path(resolved_remote).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target.path, resolved_remote)

            self._prune_backups(app_name)

            return DeployResult(
                success=True,
                file_hash=target.file_hash,
                backup_path=target.path,
            )
        except OSError as e:
            return DeployResult(
                success=False,
                error_message=f"Local rollback failed: {e}",
            )

    def get_status(self) -> dict[str, AppStatus]:
        """Return status only for apps whose file actually exists on the target.

        Matches CSP/SSH semantics: get_status() reports what's really
        there, not what the config says should be there. An app that's
        configured but has never been pushed is absent from the result,
        and the CLI status command renders it as "not deployed" via the
        history-fallback path.
        """
        result: dict[str, AppStatus] = {}
        for app_name, cfg in self._apps.items():
            remote_path = cfg.get("remote", "")
            if not remote_path:
                continue
            resolved = self._resolve_remote(remote_path)
            if not os.path.exists(resolved):
                continue
            result[app_name] = AppStatus(
                app_name=app_name,
                running=True,  # file on disk == "running" for local transport
                file_hash=compute_file_hash(resolved),
                remote_path=remote_path,
            )
        return result

    def list_backups(self, app_name: str) -> list[BackupInfo]:
        app_dir = self._app_backup_dir(app_name)
        if not app_dir.exists():
            return []

        infos: list[BackupInfo] = []
        for entry in app_dir.iterdir():
            m = _BACKUP_RE.match(entry.name)
            if not m:
                continue
            date = m.group("date")
            time = m.group("time")
            file_hash = m.group("hash")
            timestamp = (
                f"{date[0:4]}-{date[4:6]}-{date[6:8]}T"
                f"{time[0:2]}:{time[2:4]}:{time[4:6]}"
            )
            infos.append(BackupInfo(
                version=f"{date}-{time}",
                timestamp=timestamp,
                file_hash=file_hash,
                path=str(entry),
            ))

        infos.sort(key=lambda b: b.timestamp, reverse=True)
        return infos

    def get_logs(
        self,
        app_name: str,
        service: str,
        lines: int = 100,
    ) -> Optional[str]:
        return None

    def exec_command(
        self,
        command: str,
        timeout: Optional[float] = None,
    ) -> tuple[int, str, str]:
        """Run a shell command on the local machine.

        Used by `satdeploy validate` against the local transport (demo,
        chroot, mounted rootfs). The shell-interpreted string sees the
        host's environment — it is the user's responsibility to make the
        validate_command portable across "real SSH target" and
        "local-transport stand-in".
        """
        import subprocess
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise TransportError(
                f"Command timed out after {timeout}s: {command}"
            ) from e
        return proc.returncode, proc.stdout, proc.stderr
