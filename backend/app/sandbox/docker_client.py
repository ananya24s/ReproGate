"""Low-level Docker daemon access: image, container, and log operations.

This is the only module permitted to talk to the Docker daemon.

Commands are always argument vectors handed straight to ``exec``. No shell is
involved anywhere in this module, so no repository-supplied string can ever be
interpreted as a command.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from app.core.config import Settings, get_settings
from app.core.exceptions import SandboxError
from app.core.logging import get_logger
from app.sandbox.limits import SandboxLimits
from app.sandbox.models import ExecOutcome

logger = get_logger(__name__)


@dataclass(frozen=True)
class ContainerHandle:
    """A created container, plus the identity needed to clean it up."""

    id: str
    image: str
    container: Any
    """The SDK's container object. Never handed outside this module."""

    @property
    def short_id(self) -> str:
        return self.id[:12]


class SandboxDockerClient:
    """A narrow, async-friendly wrapper over the Docker SDK.

    The SDK is synchronous, so every call runs in a worker thread. The client
    object may be injected, which is what lets the runner be tested without a
    daemon.
    """

    def __init__(
        self,
        docker_client: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = docker_client
        self._owns_client = docker_client is None

    def _require_client(self) -> Any:
        """Connect to the daemon on first use."""
        if self._client is not None:
            return self._client

        # Imported lazily so this module can be imported without the SDK
        # present and without a daemon running.
        try:
            import docker
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise SandboxError(
                "The docker SDK is not installed; sandbox execution is unavailable."
            ) from exc

        try:
            self._client = docker.from_env()
        except Exception as exc:
            raise SandboxError(f"The Docker daemon is not reachable: {exc}") from exc
        return self._client

    async def ping(self) -> bool:
        """Whether the daemon answers."""
        try:
            client = self._require_client()
            return bool(await asyncio.to_thread(client.ping))
        except SandboxError:
            return False
        except Exception:
            return False

    async def ensure_image(self, image: str) -> None:
        """Make sure ``image`` is present locally, pulling it if permitted."""
        client = self._require_client()

        try:
            await asyncio.to_thread(client.images.get, image)
            return
        except Exception:
            # Absent locally; fall through to the pull.
            pass

        if not self._settings.sandbox_pull_missing_image:
            raise SandboxError(
                f"The image {image!r} is not present locally and pulling is disabled."
            )

        logger.info("Pulling sandbox image", extra={"image": image})
        try:
            await asyncio.to_thread(client.images.pull, image)
        except Exception as exc:
            raise SandboxError(
                f"The image {image!r} could not be pulled: {exc}"
            ) from exc

    async def create_container(
        self,
        *,
        image: str,
        workspace_host_path: str,
        workspace_container_path: str,
        limits: SandboxLimits,
        command: list[str],
        environment: dict[str, str] | None = None,
    ) -> ContainerHandle:
        """Create — but do not start — a confined container.

        Exactly one host path is exposed, read-write, at
        ``workspace_container_path``. The Docker socket is never mounted, and
        no other host directory is bound.
        """
        client = self._require_client()

        options = limits.container_options(
            workspace_container_path=workspace_container_path
        )
        options.update(
            {
                "image": image,
                "command": command,
                "environment": environment or {},
                "volumes": {
                    workspace_host_path: {
                        "bind": workspace_container_path,
                        "mode": "rw",
                    }
                },
            }
        )

        try:
            container = await asyncio.to_thread(client.containers.create, **options)
        except Exception as exc:
            raise SandboxError(
                f"The sandbox container could not be created: {exc}"
            ) from exc

        handle = ContainerHandle(
            id=getattr(container, "id", "") or "", image=image, container=container
        )
        logger.info(
            "Created sandbox container",
            extra={
                "container_id": handle.short_id,
                "image": image,
                "memory_limit_mb": limits.memory_limit_mb,
                "cpu_limit": limits.cpu_limit,
                "pids_limit": limits.pids_limit,
                "user": options["user"],
            },
        )
        return handle

    async def start(self, handle: ContainerHandle) -> None:
        """Start a created container."""
        try:
            await asyncio.to_thread(handle.container.start)
        except Exception as exc:
            raise SandboxError(
                f"The sandbox container could not be started: {exc}"
            ) from exc

    async def exec(
        self,
        handle: ContainerHandle,
        argv: list[str],
        *,
        workdir: str,
        timeout: float,
        environment: dict[str, str] | None = None,
        user: str | None = None,
        max_output_bytes: int = 1_000_000,
    ) -> ExecOutcome:
        """Run one command inside the container.

        Args:
            argv: An argument vector. Never a shell string.
            timeout: Wall-clock limit. On expiry the container is killed, which
                is what makes the blocked exec return.

        Returns:
            The exit code and captured streams, or a timed-out outcome.
        """
        started = time.monotonic()

        def _run() -> tuple[int | None, bytes, bytes]:
            exit_code, output = handle.container.exec_run(
                cmd=argv,
                workdir=workdir,
                environment=environment or {},
                user=user or "",
                demux=True,
                stream=False,
                tty=False,
            )
            stdout, stderr = output if isinstance(output, tuple) else (output, b"")
            return exit_code, stdout or b"", stderr or b""

        try:
            exit_code, stdout, stderr = await asyncio.wait_for(
                asyncio.to_thread(_run), timeout=timeout
            )
        except TimeoutError:
            # The worker thread stays blocked until the exec returns, so kill
            # the container to unblock it. It is disposable by design.
            await self.kill(handle)
            elapsed = time.monotonic() - started
            logger.warning(
                "Sandbox command timed out",
                extra={
                    "container_id": handle.short_id,
                    "command": argv[0],
                    "timeout_seconds": timeout,
                },
            )
            return ExecOutcome(
                exit_code=None,
                timed_out=True,
                duration_ms=int(elapsed * 1000),
                stderr=f"Command timed out after {timeout:.0f}s.",
            )
        except Exception as exc:
            raise SandboxError(f"A sandbox command could not be run: {exc}") from exc

        return ExecOutcome(
            exit_code=exit_code,
            stdout=_decode(stdout, max_output_bytes),
            stderr=_decode(stderr, max_output_bytes),
            timed_out=False,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def disable_network(self, handle: ContainerHandle) -> bool:
        """Detach the container from every network it is attached to.

        Returns:
            Whether the container is now certain to be offline.
        """
        client = self._require_client()

        def _disconnect() -> bool:
            handle.container.reload()
            settings = handle.container.attrs.get("NetworkSettings", {})
            networks = settings.get("Networks", {}) or {}
            if not networks:
                return True

            for name in list(networks):
                network = client.networks.get(name)
                network.disconnect(handle.container, force=True)
            return True

        try:
            result = await asyncio.to_thread(_disconnect)
        except Exception as exc:
            logger.warning(
                "Could not detach the sandbox container from the network",
                extra={"container_id": handle.short_id, "error": str(exc)},
            )
            return False

        logger.info("Sandbox network disabled", extra={"container_id": handle.short_id})
        return bool(result)

    async def kill(self, handle: ContainerHandle) -> None:
        """Kill a running container, ignoring one that has already stopped."""
        try:
            await asyncio.to_thread(handle.container.kill)
        except Exception:  # pragma: no cover - already dead is the common case
            logger.debug("Sandbox container was already stopped")

    async def remove(self, handle: ContainerHandle) -> None:
        """Force-remove a container and its anonymous volumes."""
        try:
            await asyncio.to_thread(handle.container.remove, force=True, v=True)
        except Exception as exc:
            raise SandboxError(
                f"The sandbox container {handle.short_id} could not be removed: {exc}"
            ) from exc

    async def close(self) -> None:
        """Release the SDK client, if this instance created it."""
        if self._client is not None and self._owns_client:
            with suppress(Exception):  # best effort; nothing depends on it
                await asyncio.to_thread(self._client.close)


def _decode(payload: bytes, limit: int) -> str:
    """Decode captured output, truncating anything unreasonably large."""
    if len(payload) > limit:
        marker = f"\n… [output truncated by ReproGate at {limit} bytes]"
        return payload[:limit].decode("utf-8", errors="replace") + marker
    return payload.decode("utf-8", errors="replace")
