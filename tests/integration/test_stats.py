import tempfile
import os
import pytest

from app.core.models import Job, JobSpec, JobStatus
from app.db.repository import JobRepository


@pytest.fixture
def repo():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    r = JobRepository(db_path=db_path)
    yield r
    os.unlink(db_path)


def _job(status: JobStatus) -> Job:
    job = Job(spec=JobSpec(image="alpine:latest", command=["echo", "hi"]))
    job.status = status
    return job


class TestGetJobCountsByStatus:

    def test_empty_db_returns_empty_dict(self, repo):
        counts = repo.get_job_counts_by_status()
        assert counts == {}

    def test_single_pending_job(self, repo):
        repo.save_job(_job(JobStatus.PENDING))
        counts = repo.get_job_counts_by_status()
        assert counts == {"pending": 1}

    def test_multiple_statuses(self, repo):
        repo.save_job(_job(JobStatus.PENDING))
        repo.save_job(_job(JobStatus.PENDING))

        j = _job(JobStatus.SUCCESS)
        repo.save_job(j)

        j2 = _job(JobStatus.FAILED)
        repo.save_job(j2)

        counts = repo.get_job_counts_by_status()
        assert counts["pending"] == 2
        assert counts["success"] == 1
        assert counts["failed"] == 1
        assert "running" not in counts

    def test_total_matches_sum(self, repo):
        for _ in range(5):
            repo.save_job(_job(JobStatus.PENDING))
        for _ in range(3):
            repo.save_job(_job(JobStatus.SUCCESS))

        counts = repo.get_job_counts_by_status()
        assert sum(counts.values()) == 8