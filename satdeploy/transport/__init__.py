"""Transport layer abstraction for satdeploy."""

from satdeploy.transport.base import (
    Transport,
    TransportError,
    DeployResult,
    AppStatus,
    BackupInfo,
)
from satdeploy.transport.ssh import SSHTransport
from satdeploy.transport.csp import CSPTransport
from satdeploy.transport.local import LocalTransport

__all__ = [
    "Transport",
    "TransportError",
    "DeployResult",
    "AppStatus",
    "BackupInfo",
    "SSHTransport",
    "CSPTransport",
    "LocalTransport",
]
