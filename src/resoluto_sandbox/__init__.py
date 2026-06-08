"""Resoluto sandbox — store-mediated, Kata-isolated, cloud-agnostic execution.

Platform-independent surface (no optional deps). Concrete runtimes/stores with
platform deps import lazily:
    from resoluto_sandbox.runtime.k8s import K8sSandboxRuntime
    from resoluto_sandbox.objectstore.s3 import S3ObjectStore
"""
from resoluto_sandbox.contracts import (
    ObjectInfo,
    ObjectStore,
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SandboxStatus,
    SpanEvent,
)
from resoluto_sandbox.objectstore import LocalFsObjectStore
from resoluto_sandbox.pool import SandboxLease, SandboxPool
from resoluto_sandbox.telemetry import ChunkReader, ChunkShipper, TerminalChunkGap

__all__ = [
    "ObjectInfo", "ObjectStore", "SandboxHandle", "SandboxLaunchSpec",
    "SandboxRuntime", "SandboxStatus", "SpanEvent",
    "LocalFsObjectStore", "SandboxLease", "SandboxPool",
    "ChunkReader", "ChunkShipper", "TerminalChunkGap",
]
