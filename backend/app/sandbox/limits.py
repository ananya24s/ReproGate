"""Resource, network, filesystem, and timeout restrictions applied to every
sandboxed container.

Every restriction is expressed here and nowhere else, so what confines an
execution can be read in one place rather than inferred from call sites.
"""

from __future__ import annotations

import os
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, get_settings

#: Dropped unconditionally. A test runner needs none of them.
DROPPED_CAPABILITIES: Final = ("ALL",)

#: Applied to every container, without exception.
SECURITY_OPTIONS: Final = ("no-new-privileges:true",)

_NANO_CPUS_PER_CPU: Final = 1_000_000_000
_BYTES_PER_MB: Final = 1024 * 1024

#: Fallback when the host process is root, which must never be the container
#: user. Matches the `node` user in the official images.
_FALLBACK_USER: Final = "1000:1000"


class SandboxLimits(BaseModel):
    """The confinement applied to one sandboxed execution."""

    model_config = ConfigDict(frozen=True)

    cpu_limit: float = Field(gt=0)
    memory_limit_mb: int = Field(gt=0)
    pids_limit: int = Field(gt=0)
    install_timeout_seconds: int = Field(gt=0)
    test_timeout_seconds: int = Field(gt=0)

    network_enabled_during_install: bool = True
    """Dependency installation needs a registry; the test phase does not."""

    network_enabled_during_test: bool = False
    read_only_rootfs: bool = False
    tmpfs_size_mb: int = Field(default=256, gt=0)
    run_as_user: str | None = None
    max_output_bytes: int = Field(default=1_000_000, gt=0)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> SandboxLimits:
        """Build limits from application configuration."""
        resolved = settings or get_settings()
        return cls(
            cpu_limit=resolved.sandbox_cpu_limit,
            memory_limit_mb=resolved.sandbox_memory_limit_mb,
            pids_limit=resolved.sandbox_pids_limit,
            install_timeout_seconds=resolved.sandbox_install_timeout_seconds,
            test_timeout_seconds=resolved.sandbox_timeout_seconds,
            network_enabled_during_test=resolved.sandbox_network_enabled,
            read_only_rootfs=resolved.sandbox_read_only_rootfs,
            tmpfs_size_mb=resolved.sandbox_tmpfs_size_mb,
            run_as_user=resolved.sandbox_run_as_user,
            max_output_bytes=resolved.sandbox_max_output_bytes,
        )

    @property
    def nano_cpus(self) -> int:
        """CPU quota in the units the Docker API expects."""
        return int(self.cpu_limit * _NANO_CPUS_PER_CPU)

    @property
    def memory_limit_bytes(self) -> int:
        return self.memory_limit_mb * _BYTES_PER_MB

    @property
    def total_timeout_seconds(self) -> int:
        """Both phases plus headroom, used as the container's own dead-man timer."""
        return self.install_timeout_seconds + self.test_timeout_seconds + 60

    def resolve_run_as_user(self) -> str:
        """The ``uid:gid`` the container runs as.

        Defaults to the host user so that writes to the bind-mounted workspace
        succeed. If the host process is root — a CI container, typically — an
        unprivileged id is used instead, because running the sandbox as root
        would defeat the point.
        """
        if self.run_as_user:
            return self.run_as_user

        uid = os.getuid()
        if uid == 0:
            return _FALLBACK_USER
        return f"{uid}:{os.getgid()}"

    def container_options(self, *, workspace_container_path: str) -> dict[str, Any]:
        """Keyword arguments for container creation that enforce this policy.

        Notably absent, and never added: ``privileged``, any Docker socket
        mount, and any host path beyond the single workspace bind.
        """
        return {
            "privileged": False,
            "cap_drop": list(DROPPED_CAPABILITIES),
            "security_opt": list(SECURITY_OPTIONS),
            "nano_cpus": self.nano_cpus,
            "mem_limit": f"{self.memory_limit_mb}m",
            "pids_limit": self.pids_limit,
            "read_only": self.read_only_rootfs,
            "tmpfs": {"/tmp": f"size={self.tmpfs_size_mb}m"},
            "user": self.resolve_run_as_user(),
            "working_dir": workspace_container_path,
            "auto_remove": False,
            "detach": True,
            "network_disabled": not self.network_enabled_during_install,
        }
