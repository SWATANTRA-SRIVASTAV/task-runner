"""
Integration tests for JobRepository.

Uses a real SQLite database in a temp file — not mocked. This tests the
actual SQL, type coercions, and WAL mode behaviour. These tests are slow
(file I/O) but catch real bugs that mock-based tests miss.

Run with: pytest tests/integration/ -v
"""

import pytest
import tempfile
import os

from app.core.models import (
    FailureReason,
    Job,
    JobSpec,
    JobStatus,
    ResourceLimits,
)
from app.db.repository import JobRepository


@pytest.fixture
def repo():
    """Fresh in-memory-like DB for each test (temp file, deleted after)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    r = JobRepository(db_path=db_path)
    yield r
    os.unlink(db_path)


def _make_job(**kwargs) -> Job:
    spec = JobSpec(
        image=kwargs.pop("image", "alpine:latest"),
        command=kwargs.pop("command", ["echo", "hello"]),
        **kwargs,
    )
    return Job(spec=spec)


class TestSaveAndRetrieve:
    def test_save_then_get(self, repo):
        job = _make_job()
        repo.save_job(job)
        fetched = repo.get_job(job.id)
        assert fetched is not None
        assert fetched.id == job.id
        assert fetched.spec.image == "alpine:latest"
        assert fetched.status == JobStatus.PENDING

    def test_get_unknown_returns_none(self, repo):
        assert repo.get_job("nonexistent-id") is None

    def test_saves_resource_limits(self, repo):
        job = _make_job()
        job.spec.limits = ResourceLimits(memory_mb=512, cpu_quota=0.5)
        repo.save_job(job)
        fetched = repo.get_job(job.id)
        assert fetched.spec.limits.memory_mb == 512
        assert fetched.spec.limits.cpu_quota == 0.5

    def test_saves_env_vars(self, repo):
        job = _make_job()
        job.spec.env = {"MY_VAR": "hello", "OTHER": "world"}
        repo.save_job(job)
        fetched = repo.get_job(job.id)
        assert fetched.spec.env == {"MY_VAR": "hello", "OTHER": "world"}


class TestUpdate:
    def test_status_update_persists(self, repo):
        job = _make_job()
        repo.save_job(job)
        job.status = JobStatus.RUNNING
        job.container_id = "abc123"
        repo.update_job(job)
        fetched = repo.get_job(job.id)
        assert fetched.status == JobStatus.RUNNING
        assert fetched.container_id == "abc123"

    def test_failure_reason_persists(self, repo):
        job = _make_job()
        repo.save_job(job)
        job.status = JobStatus.FAILED
        job.failure_reason = FailureReason.OOM_KILLED
        job.exit_code = 137
        repo.update_job(job)
        fetched = repo.get_job(job.id)
        assert fetched.failure_reason == FailureReason.OOM_KILLED
        assert fetched.exit_code == 137


class TestGetPendingJobs:
    def test_returns_pending_jobs(self, repo):
        j1, j2 = _make_job(), _make_job()
        repo.save_job(j1)
        repo.save_job(j2)
        pending = repo.get_pending_jobs()
        assert len(pending) == 2

    def test_excludes_running_jobs(self, repo):
        job = _make_job()
        repo.save_job(job)
        job.status = JobStatus.RUNNING
        repo.update_job(job)
        pending = repo.get_pending_jobs()
        assert len(pending) == 0

    def test_includes_retrying_jobs(self, repo):
        job = _make_job()
        repo.save_job(job)
        job.status = JobStatus.RETRYING
        repo.update_job(job)
        pending = repo.get_pending_jobs()
        assert len(pending) == 1

    def test_respects_limit(self, repo):
        for _ in range(5):
            repo.save_job(_make_job())
        pending = repo.get_pending_jobs(limit=3)
        assert len(pending) == 3
