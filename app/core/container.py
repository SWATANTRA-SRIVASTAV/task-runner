"""
ContainerManager: single point of contact with the Docker Engine API.

Design decision: every Docker SDK call lives here. Nothing outside this
module imports docker directly. This makes it easy to mock in tests and
swap out the backend (e.g. podman-py) later.

Known failure modes handled here:
  - Image not found / pull failure → DockerOperationError
  - OOM kill (exit code 137, OOMKilled flag) → detected via inspect
  - Container already removed → graceful no-op on cleanup
  - Daemon unreachable → DockerOperationError with clear message
"""

import logging
from typing import Generator, Optional

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.models.containers import Container

from app.core.models import JobSpec, FailureReason

logger = logging.getLogger(__name__)


class DockerOperationError(Exception):
    """Wraps Docker SDK errors with context about what we were trying to do."""
    pass


class ContainerResult:
    def __init__(self, exit_code: int, oom_killed: bool):
        self.exit_code = exit_code
        self.oom_killed = oom_killed

    @property
    def failure_reason(self) -> Optional[FailureReason]:
        if self.oom_killed:
            return FailureReason.OOM_KILLED
        if self.exit_code != 0:
            return FailureReason.EXIT_CODE
        return None

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.oom_killed


class ContainerManager:
    def __init__(self, docker_url: Optional[str] = None):
        """
        docker_url: e.g. "unix:///var/run/docker.sock" (default)
        Explicitly passing the URL makes it testable with a mock server.
        """
        try:
            kwargs = {"timeout": 10}
            if docker_url:
                kwargs["base_url"] = docker_url
            self._client = docker.from_env(**kwargs) if not docker_url else docker.DockerClient(**kwargs)
            self._client.ping()
        except Exception as exc:
            raise DockerOperationError(
                f"Cannot reach Docker daemon. Is Docker running? Detail: {exc}"
            ) from exc

    def pull_image_if_missing(self, image: str) -> None:
        """
        Only pulls if not cached locally. Avoids the latency hit on every job.
        Raises DockerOperationError on pull failure (bad tag, no registry access).
        """
        try:
            self._client.images.get(image)
            logger.debug("Image %s found in local cache, skipping pull", image)
        except ImageNotFound:
            logger.info("Image %s not found locally, pulling...", image)
            try:
                self._client.images.pull(image)
            except APIError as exc:
                raise DockerOperationError(f"Failed to pull image '{image}': {exc}") from exc

    def create_container(self, job_id: str, spec: JobSpec) -> str:
        """
        Creates (but does not start) a container. Returns container ID.

        We separate create from start so we can record the container ID
        in the DB before the process is alive — this prevents orphaned
        containers if the runner crashes between create and start.
        """
        host_config = spec.limits.to_docker_host_config()
        env_list = [f"{k}={v}" for k, v in spec.env.items()]

        try:
            container: Container = self._client.containers.create(
                image=spec.image,
                command=spec.command,
                environment=env_list,
                name=f"taskrunner-{job_id}",
                labels={"task-runner.job-id": job_id},
                # Security hardening — drop all capabilities, no new privs
                # See docs/decisions/002-container-security.md
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                network_mode="none",   # no network by default; enable per job spec if needed
                **host_config,
            )
            return container.id
        except APIError as exc:
            raise DockerOperationError(f"Failed to create container for job {job_id}: {exc}") from exc

    def start_container(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
            container.start()
        except NotFound:
            raise DockerOperationError(f"Container {container_id} not found — was it removed?")
        except APIError as exc:
            raise DockerOperationError(f"Failed to start container {container_id}: {exc}") from exc

    def wait_for_container(self, container_id: str, timeout: Optional[int] = None) -> ContainerResult:
        """
        Blocks until the container exits. Returns exit code and OOM status.

        IMPORTANT: We must call inspect *after* wait, not before — the
        OOMKilled flag is only set in the state once the container has stopped.
        """
        try:
            container = self._client.containers.get(container_id)
            # wait() returns {"StatusCode": N, "Error": ...}
            result = container.wait(timeout=timeout)
            exit_code = result.get("StatusCode", -1)

            # Reload state to get OOMKilled flag
            container.reload()
            oom_killed = container.attrs.get("State", {}).get("OOMKilled", False)

            if oom_killed:
                logger.warning(
                    "Container %s was OOM-killed (memory limit: enforced by cgroups v2 memory.max)",
                    container_id,
                )

            return ContainerResult(exit_code=exit_code, oom_killed=oom_killed)

        except Exception as exc:
            error_str = str(exc).lower()
            if "timeout" in error_str or "timed out" in error_str or "readtimeout" in error_str:
                raise TimeoutError(f"Container {container_id} timed out after {timeout}s") from exc
            raise DockerOperationError(f"Error waiting for container {container_id}: {exc}") from exc

    def stream_logs(self, container_id: str) -> Generator[str, None, None]:
        """
        Yields log lines as they arrive. Uses Docker SDK streaming to avoid
        buffering the entire output — important for long-running jobs.

        Each yielded string ends with \\n (Docker multiplexed stream includes it).
        """
        try:
            container = self._client.containers.get(container_id)
            for chunk in container.logs(stream=True, follow=True, timestamps=True):
                yield chunk.decode("utf-8", errors="replace")
        except NotFound:
            logger.warning("Container %s not found when streaming logs", container_id)
        except APIError as exc:
            logger.error("Log stream error for container %s: %s", container_id, exc)

    def stop_container(self, container_id: str, timeout_seconds: int = 5) -> None:
        """
        Sends SIGTERM to the container process, waits up to timeout_seconds,
        then sends SIGKILL if it hasn't stopped.

        Used in the timeout path — we want a clean shutdown attempt before
        force-removing. Some jobs trap SIGTERM and flush state cleanly.
        We give them 5 seconds then kill regardless.
        """
        try:
            container = self._client.containers.get(container_id)
            container.stop(timeout=timeout_seconds)
            logger.info("Stopped container %s (SIGTERM + grace period)", container_id)
        except NotFound:
            logger.debug("Container %s already gone when stop was called", container_id)
        except APIError as exc:
            logger.error("Failed to stop container %s: %s — will attempt force remove", container_id, exc)
    
    def remove_container(self, container_id: str, force: bool = False) -> None:
        """
        Idempotent — silently ignores NotFound. Safe to call in finally blocks.
        force=True kills a running container before removal (used in cancel path).
        """
        try:
            container = self._client.containers.get(container_id)
            container.remove(force=force)
            logger.debug("Removed container %s", container_id)
        except NotFound:
            logger.debug("Container %s already gone, nothing to remove", container_id)
        except APIError as exc:
            # Log but don't raise — cleanup failure shouldn't crash the caller
            logger.error("Failed to remove container %s: %s", container_id, exc)

    def close(self) -> None:
        self._client.close()
