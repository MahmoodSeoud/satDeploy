"""SSH connection wrapper for satdeploy."""

import socket
from dataclasses import dataclass
from typing import Optional

import paramiko


class SSHError(Exception):
    """Exception raised for SSH connection and command failures."""

    pass


@dataclass
class CommandResult:
    """Result of running a remote command."""

    stdout: str
    stderr: str
    exit_code: int


class SSHClient:
    """SSH client wrapper for remote operations."""

    def __init__(self, host: str, user: str, port: int = 22):
        self.host = host
        self.user = user
        self.port = port
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None

    def connect(self) -> None:
        """Establish SSH connection.

        Raises:
            SSHError: If connection fails for any reason.
        """
        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self._client.connect(
                hostname=self.host,
                port=self.port,
                username=self.user,
            )
        except paramiko.AuthenticationException as e:
            raise SSHError(f"Authentication failed for {self.user}@{self.host}: {e}")
        except paramiko.SSHException as e:
            raise SSHError(f"SSH error connecting to {self.host}: {e}")
        except socket.timeout as e:
            raise SSHError(f"Connection timed out to {self.host}: {e}")
        except socket.error as e:
            raise SSHError(f"Connection failed to {self.host}: {e}")

    def disconnect(self) -> None:
        """Close SSH connection."""
        if self._sftp:
            self._sftp.close()
            self._sftp = None
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "SSHClient":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()

    def run(self, command: str, check: bool = True) -> CommandResult:
        """Run a command on the remote host.

        Args:
            command: The command to run.
            check: If True, raise SSHError on non-zero exit code.

        Returns:
            CommandResult with stdout, stderr, and exit code.

        Raises:
            SSHError: If check=True and command exits with non-zero code.
        """
        if not self._client:
            raise SSHError("Not connected")

        _, stdout, stderr = self._client.exec_command(command)
        exit_code = stdout.channel.recv_exit_status()
        stdout_text = stdout.read().decode()
        stderr_text = stderr.read().decode()

        result = CommandResult(
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=exit_code,
        )

        if check and exit_code != 0:
            raise SSHError(f"Command failed: {stderr_text}")

        return result

    def _get_sftp(self) -> paramiko.SFTPClient:
        """Get or create SFTP client."""
        if not self._client:
            raise SSHError("Not connected")
        if not self._sftp:
            self._sftp = self._client.open_sftp()
        return self._sftp

    def upload(self, local_path: str, remote_path: str) -> None:
        """Upload a local file to the remote host.

        Args:
            local_path: Path to the local file.
            remote_path: Path on the remote host.
        """
        sftp = self._get_sftp()
        sftp.put(local_path, remote_path)

    def download(self, remote_path: str, local_path: str) -> None:
        """Download a remote file to the local host.

        Args:
            remote_path: Path on the remote host.
            local_path: Path to save locally.
        """
        sftp = self._get_sftp()
        sftp.get(remote_path, local_path)

    def file_exists(self, remote_path: str) -> bool:
        """Check if a file exists on the remote host.

        Args:
            remote_path: Path to check.

        Returns:
            True if the file exists, False otherwise.
        """
        sftp = self._get_sftp()
        try:
            sftp.stat(remote_path)
            return True
        except (FileNotFoundError, IOError):
            return False

    def copy_remote(self, src: str, dst: str) -> None:
        """Copy a file on the remote host.

        Args:
            src: Source path.
            dst: Destination path.
        """
        self.run(f"cp '{src}' '{dst}'")
