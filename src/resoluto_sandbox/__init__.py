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
from resoluto_sandbox.driver import drive_node
from resoluto_sandbox.objectstore import LocalFsObjectStore
from resoluto_sandbox.pool import SandboxLease, SandboxPool
from resoluto_sandbox.runner import run_node_in_sandbox
from resoluto_sandbox.spans import SpanEmitter
from resoluto_sandbox.staging import (
    collect_outputs,
    fetch_outputs,
    put_dir,
    stage_inputs,
)
from resoluto_sandbox.telemetry import ChunkReader, ChunkShipper, TerminalChunkGap

__all__ = [
    "ObjectInfo", "ObjectStore", "SandboxHandle", "SandboxLaunchSpec",
    "SandboxRuntime", "SandboxStatus", "SpanEvent",
    "LocalFsObjectStore", "SandboxLease", "SandboxPool",
    "ChunkReader", "ChunkShipper", "TerminalChunkGap",
    "SpanEmitter", "run_node_in_sandbox", "drive_node",
    "put_dir", "stage_inputs", "collect_outputs", "fetch_outputs",
]
