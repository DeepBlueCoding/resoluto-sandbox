"""Resoluto sandbox — store-mediated, Kata-isolated execution; concrete runtimes/conduits with platform deps import lazily."""

from resoluto.sandbox.backends.base import Backend, RunResult
from resoluto.sandbox.backends.substrate import SubstrateBackend
from resoluto.sandbox.client import Sandbox
from resoluto.sandbox.conduit import LocalConduit, StdoutConduit
from resoluto.sandbox.contracts import (
    Admission,
    Conduit,
    Lease,
    NodeResult,
    ObjectInfo,
    SandboxHandle,
    SandboxLaunchSpec,
    SandboxRuntime,
    SandboxStatus,
    SpanEvent,
)
from resoluto.sandbox.driver import NodeOutcome, drive_node, drive_node_raw
from resoluto.sandbox.runner import run_node_in_sandbox
from resoluto.sandbox.spans import SpanEmitter
from resoluto.sandbox.staging import (
    collect_outputs,
    fetch_outputs,
    put_dir,
    stage_inputs,
)
from resoluto.sandbox.telemetry import ChunkReader, ChunkShipper

__all__ = [
    "Sandbox",
    "RunResult",
    "Backend",
    "SubstrateBackend",
    "NodeResult",
    "ObjectInfo",
    "Conduit",
    "SandboxHandle",
    "SandboxLaunchSpec",
    "SandboxRuntime",
    "SandboxStatus",
    "SpanEvent",
    "Admission",
    "Lease",
    "LocalConduit",
    "StdoutConduit",
    "ChunkReader",
    "ChunkShipper",
    "SpanEmitter",
    "run_node_in_sandbox",
    "drive_node",
    "drive_node_raw",
    "NodeOutcome",
    "put_dir",
    "stage_inputs",
    "collect_outputs",
    "fetch_outputs",
]
