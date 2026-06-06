"""
REST API routes.

POST   /jobs              — submit a new job
GET    /jobs              — list jobs (filterable by status)
GET    /jobs/{id}         — get job detail
DELETE /jobs/{id}         — cancel a running or pending job
GET    /jobs/{id}/logs    — stream container logs (Server-Sent Events)
GET    /healthz           — liveness probe (used by Docker HEALTHCHECK)
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.schemas import JobListResponse, JobResponse, SubmitJobRequest
from app.core.container import ContainerManager
from app.core.models import Job, JobSpec, JobStatus, ResourceLimits
from app.db.repository import JobRepository

logger = logging.getLogger(__name__)
router = APIRouter()


# ------------------------------------------------------------------ #
#  Dependency injection helpers                                        #
# ------------------------------------------------------------------ #
#
#  FastAPI's Depends() system is used here to make the repo and
#  container manager injectable — in tests, you swap these out for
#  fakes without touching route code.

def get_repository() -> JobRepository:
    # Replaced at startup by app.main with the real instance
    raise NotImplementedError("Repository not injected")


def get_container_manager() -> ContainerManager:
    raise NotImplementedError("ContainerManager not injected")


# ------------------------------------------------------------------ #
#  Routes                                                              #
# ------------------------------------------------------------------ #

@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.post("/jobs", response_model=JobResponse, status_code=202)
async def submit_job(
    body: SubmitJobRequest,
    repo: JobRepository = Depends(get_repository),
):
    """
    Accepts a job spec, persists it as PENDING, returns immediately.
    The scheduler picks it up on the next poll cycle.
    """
    spec = JobSpec(
        image=body.image,
        command=body.command,
        env=body.env,
        limits=ResourceLimits(
            memory_mb=body.limits.memory_mb,
            cpu_quota=body.limits.cpu_quota,
        ),
        max_retries=body.max_retries,
        timeout_seconds=body.timeout_seconds,
    )
    job = Job(spec=spec)
    repo.save_job(job)
    logger.info("Accepted job %s (image=%s)", job.id, spec.image)
    return _to_response(job)


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    repo: JobRepository = Depends(get_repository),
):
    status_enum = None
    if status:
        try:
            status_enum = JobStatus(status)
        except ValueError:
            raise HTTPException(400, f"Unknown status '{status}'")

    jobs = repo.list_jobs(status=status_enum, limit=limit, offset=offset)
    return JobListResponse(jobs=[_to_response(j) for j in jobs], total=len(jobs))


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, repo: JobRepository = Depends(get_repository)):
    job = repo.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return _to_response(job)


@router.delete("/jobs/{job_id}", status_code=202)
async def cancel_job(
    job_id: str,
    repo: JobRepository = Depends(get_repository),
    containers: ContainerManager = Depends(get_container_manager),
):
    """
    Cancels a PENDING or RUNNING job.
    - PENDING: mark cancelled before container is created
    - RUNNING: force-remove the container, then mark cancelled
    """
    job = repo.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    if job.is_terminal():
        raise HTTPException(409, f"Job {job_id} is already in terminal state '{job.status}'")

    if job.container_id and job.status == JobStatus.RUNNING:
        containers.remove_container(job.container_id, force=True)
        logger.info("Force-removed container %s for job %s", job.container_id, job_id)

    job.status = JobStatus.CANCELLED
    repo.update_job(job)
    return {"id": job_id, "status": "cancelled"}


@router.get("/jobs/{job_id}/logs")
async def stream_logs(
    job_id: str,
    repo: JobRepository = Depends(get_repository),
    containers: ContainerManager = Depends(get_container_manager),
):
    """
    Streams container logs as Server-Sent Events.
    Returns 404 if job not found, 409 if the container hasn't started yet.

    The StreamingResponse keeps the HTTP connection open and yields log
    lines as they arrive from the Docker SDK. This avoids loading the
    entire log into memory for long-running jobs.
    """
    job = repo.get_job(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    if not job.container_id:
        raise HTTPException(409, f"Job {job_id} has no container yet (status: {job.status})")

    def _log_generator():
        for line in containers.stream_logs(job.container_id):
            yield f"data: {line}\n\n"

    return StreamingResponse(
        _log_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ------------------------------------------------------------------ #
#  Internal helpers                                                    #
# ------------------------------------------------------------------ #

def _to_response(job: Job) -> JobResponse:
    return JobResponse(
        id=job.id,
        status=job.status.value,
        image=job.spec.image,
        command=job.spec.command,
        attempt=job.attempt,
        exit_code=job.exit_code,
        failure_reason=job.failure_reason.value if job.failure_reason else None,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )
