import asyncio
import tempfile
import os
import pytest
from datetime import datetime, timezone

from app.core.models import Job, JobSpec, JobStatus, FailureReason
from app.db.repository import JobRepository


def _make_running_job(container_id: str = "abc123deadbeef") -> Job:
    job = Job(spec=JobSpec(image="alpine:latest", command=["sleep", "999"]))
    job.status = JobStatus.RUNNING
    job.container_id = container_id
    job.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return job


class TestReconciliationLogic:
    """
    Tests the DB state transitions that reconciliation performs.
    No Docker daemon needed — we test the repository behavior directly.
    """

    def setup_method(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.repo = JobRepository(db_path=self._tmp.name)

    def teardown_method(self):
        os.unlink(self._tmp.name)

    def test_running_job_exists_in_db(self):
        job = _make_running_job()
        self.repo.save_job(job)
        fetched = self.repo.get_job(job.id)
        assert fetched.status == JobStatus.RUNNING
        assert fetched.container_id == "abc123deadbeef"

    def test_marking_orphaned_job_failed(self):
        job = _make_running_job()
        self.repo.save_job(job)

        # simulate what reconcile_orphaned_jobs does
        job.status = JobStatus.FAILED
        job.failure_reason = FailureReason.DOCKER_ERROR
        job.error_message = "Orphaned on daemon restart — container was already gone"
        job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.repo.update_job(job)

        fetched = self.repo.get_job(job.id)
        assert fetched.status == JobStatus.FAILED
        assert fetched.failure_reason == FailureReason.DOCKER_ERROR
        assert fetched.finished_at is not None
        assert "Orphaned" in fetched.error_message

    def test_orphaned_job_does_not_appear_in_pending_queue(self):
        job = _make_running_job()
        self.repo.save_job(job)

        job.status = JobStatus.FAILED
        job.failure_reason = FailureReason.DOCKER_ERROR
        job.error_message = "Orphaned on daemon restart — container was already gone"
        job.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.repo.update_job(job)

        pending = self.repo.get_pending_jobs()
        assert all(j.id != job.id for j in pending)

    def test_non_running_jobs_are_not_touched(self):
        success_job = Job(spec=JobSpec(image="alpine:latest", command=["echo", "hi"]))
        success_job.status = JobStatus.SUCCESS
        self.repo.save_job(success_job)

        pending_job = Job(spec=JobSpec(image="alpine:latest", command=["echo", "hi"]))
        self.repo.save_job(pending_job)

        # reconciliation only touches RUNNING jobs
        running = self.repo.list_jobs(status=JobStatus.RUNNING)
        assert len(running) == 0
        assert self.repo.get_job(success_job.id).status == JobStatus.SUCCESS
        assert self.repo.get_job(pending_job.id).status == JobStatus.PENDING