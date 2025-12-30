"""Protobuf message definitions for CSP deploy protocol."""

from satdeploy.csp.proto.deploy_pb2 import (
    DeployCommand,
    DeployRequest,
    DeployResponse,
    DeployError,
    BackupEntry,
    AppStatusEntry,
)

__all__ = [
    "DeployCommand",
    "DeployRequest",
    "DeployResponse",
    "DeployError",
    "BackupEntry",
    "AppStatusEntry",
]
