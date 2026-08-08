"""Executes untrusted repository code inside short-lived Docker containers with
explicit resource, network, filesystem, and timeout restrictions."""

from app.sandbox.docker_client import ContainerHandle, SandboxDockerClient
from app.sandbox.limits import SandboxLimits
from app.sandbox.models import (
    CleanupReport,
    CleanupStatus,
    ExecOutcome,
    ExecutionPhase,
    InfrastructureStatus,
    SandboxExecutionRequest,
    SandboxExecutionResult,
    TestReport,
    TestStatus,
    TimeoutInfo,
)
from app.sandbox.runner import SandboxRunner

__all__ = [
    "CleanupReport",
    "CleanupStatus",
    "ContainerHandle",
    "ExecOutcome",
    "ExecutionPhase",
    "InfrastructureStatus",
    "SandboxDockerClient",
    "SandboxExecutionRequest",
    "SandboxExecutionResult",
    "SandboxLimits",
    "SandboxRunner",
    "TestReport",
    "TestStatus",
    "TimeoutInfo",
]
