"""
JobScheduler: pulls jobs from the queue and runs them in containers.

Lifecycle of a job through this module:
  PENDING → RUNNING → SUCCESS | FAILED | RETRYING → (retry) → ...

The scheduler runs as a background asyncio task. It deliberately uses
asyncio.to_thread() for all blocking Docker SDK calls — the SDK is
synchronous and would block the event loop otherwise, making the REST
API unresponsive during long container waits.

Signal handling: SIGTERM and SIGINT trigger graceful shutdown — the
scheduler finishes the current job dispatch cycle, cancels in-flight
containers, and exits cleanly. Without this, a ctrl-C during a run
leaves orphaned containers.
"""

import asyncio
import logging
import signal
from datetime import datetime, timezone
from typing import Optional

from app.core.container import ContainerManager, DockerOperationError
from app.core.models import FailureReason, Job, JobStatus
from app.core.retry import RetryPolicy, is_retryable, wait_before_retry
from app.db.repository import JobRepository

logger = logging.getLogger(__name__)


class JobScheduler:
    def __init__(
        self,
        repository: JobRepository,
        container_manager: ContainerManager,
        poll_interval_seconds: float = 1.0,
        max_concurrent_jobs: int = 4,
    ):
        self._repo = repository
        self._containers = container_manager
        self._poll_interval = poll_interval_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent_jobs)
        self._running = False
        self._active_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ #
    #  Public interface                                                    #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._running = True
        self._register_signal_handlers()
        await self.reconcile_orphaned_jobs()
        logger.info("Scheduler started (max_concurrent=%d)", self._semaphore._value)
        await self._dispatch_loop()

    async def reconcile_orphaned_jobs(self) -> None:
        running_jobs = await asyncio.to_thread(
            self._repo.list_jobs, status=JobStatus.RUNNING
        )
        if not running_jobs:
            logger.info("Reconciliation: no orphaned jobs found")
            return

        logger.warning(
            "Reconciliation: found %d job(s) stuck in RUNNING — daemon likely crashed",
            len(running_jobs),
        )

        for job in running_jobs:
            container_still_alive = False

            if job.container_id:
                try:
                    await asyncio.to_thread(
                        self._containers._client.containers.get, job.container_id
                    )
                    container_still_alive = True
                    logger.warning(
                        "Reconciliation: container %s still alive for job %s — force removing",
                        job.container_id[:12],
                        job.id,
                    )
                    await asyncio.to_thread(
                        self._containers.remove_container, job.container_id, True
                    )
                except Exception:
                    logger.info(
                        "Reconciliation: container %s already gone for job %s",
                        job.container_id[:12],
                        job.id,
                    )

            job.status = JobStatus.FAILED
            job.failure_reason = FailureReason.DOCKER_ERROR
            job.error_message = (
                "Orphaned on daemon restart — container was still alive and force-killed"
                if container_still_alive
                else "Orphaned on daemon restart — container was already gone"
            )
            job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await asyncio.to_thread(self._repo.update_job, job)
            logger.warning("Reconciliation: marked job %s as FAILED", job.id)

    async def shutdown(self) -> None:
        """
        Stops accepting new jobs, waits for in-flight jobs to finish (or
        cancels their containers if they don't finish within the grace period).
        """
        logger.info("Scheduler shutting down — waiting for %d active jobs", len(self._active_tasks))
        self._running = False

        if self._active_tasks:
            grace = 30  # seconds
            done, pending = await asyncio.wait(
                self._active_tasks.values(),
                timeout=grace,
            )
            if pending:
                logger.warning(
                    "%d jobs did not finish within %ss grace period — cancelling containers",
                    len(pending), grace,
                )
                for task in pending:
                    task.cancel()

        logger.info("Scheduler stopped cleanly")

    # ------------------------------------------------------------------ #
    #  Internal dispatch loop                                              #
    # ------------------------------------------------------------------ #

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                pending_jobs = await asyncio.to_thread(self._repo.get_pending_jobs, limit=10)
                for job in pending_jobs:
                    if not self._running:
                        break
                    task = asyncio.create_task(self._run_job(job))
                    self._active_tasks[job.id] = task
                    task.add_done_callback(lambda t, jid=job.id: self._active_tasks.pop(jid, None))
            except Exception:
                logger.exception("Unexpected error in dispatch loop")

            await asyncio.sleep(self._poll_interval)

    async def _run_job(self, job: Job) -> None:
        """Full lifecycle for a single job, including retries."""
        async with self._semaphore:
            policy = RetryPolicy(max_retries=job.spec.max_retries)

            for attempt in range(policy.max_retries + 1):
                job.attempt = attempt
                success = await self._execute_attempt(job)

                if success:
                    return

                # Check whether we should retry
                if attempt < policy.max_retries and is_retryable(job.failure_reason):
                    job.status = JobStatus.RETRYING
                    await asyncio.to_thread(self._repo.update_job, job)
                    logger.info(
                        "Job %s attempt %d failed (%s), retrying...",
                        job.id, attempt, job.failure_reason,
                    )
                    await wait_before_retry(attempt, policy)
                else:
                    # Either exhausted retries or non-retryable failure
                    job.status = JobStatus.FAILED
                    job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    await asyncio.to_thread(self._repo.update_job, job)
                    logger.warning(
                        "Job %s permanently failed after %d attempt(s): %s",
                        job.id, attempt + 1, job.failure_reason,
                    )
                    return

    async def _execute_attempt(self, job: Job) -> bool:
        """
        Runs a single container attempt. Returns True on success.
        Always cleans up the container regardless of outcome.
        """
        container_id: Optional[str] = None

        try:
            # --- Image pull (cached after first time) ---
            await asyncio.to_thread(
                self._containers.pull_image_if_missing, job.spec.image
            )

            # --- Create container BEFORE updating status ---
            # We record the container_id first so that if we crash between
            # create and start, the cleanup task can find orphaned containers.
            container_id = await asyncio.to_thread(
                self._containers.create_container, job.id, job.spec
            )
            job.container_id = container_id
            job.status = JobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await asyncio.to_thread(self._repo.update_job, job)

            # --- Start ---
            await asyncio.to_thread(self._containers.start_container, container_id)

            # --- Wait (blocking, but offloaded to thread pool) ---
            result = await asyncio.to_thread(
                self._containers.wait_for_container,
                container_id,
                job.spec.timeout_seconds,
            )

            job.exit_code = result.exit_code
            job.failure_reason = result.failure_reason

            if result.succeeded:
                job.status = JobStatus.SUCCESS
                job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
                await asyncio.to_thread(self._repo.update_job, job)
                logger.info("Job %s succeeded (exit 0)", job.id)
                return True
            else:
                return False  # caller decides whether to retry

        except TimeoutError:
            job.failure_reason = FailureReason.TIMEOUT
            job.error_message = f"Timed out after {job.spec.timeout_seconds}s"
            return False

        except DockerOperationError as exc:
            job.failure_reason = FailureReason.DOCKER_ERROR
            job.error_message = str(exc)
            logger.error("Docker error for job %s: %s", job.id, exc)
            return False

        finally:
            # Container cleanup always runs — even on exception.
            # This is the fix for the zombie container bug documented in
            # https://github.com/you/task-runner/issues/1
            if container_id:
                await asyncio.to_thread(
                    self._containers.remove_container, container_id, True
                )

    def _register_signal_handlers(self) -> None:
        """
        Registers SIGTERM and SIGINT handlers so a ctrl-C or systemd stop
        triggers graceful shutdown instead of hard kill.

        Without this, running containers become orphans — Docker doesn't
        know they were started by us, so they just keep running.
        """
        loop = asyncio.get_event_loop()

        def _handle_signal():
            logger.info("Shutdown signal received")
            asyncio.create_task(self.shutdown())

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _handle_signal)
