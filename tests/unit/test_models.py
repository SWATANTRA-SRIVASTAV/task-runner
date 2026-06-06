"""
Tests for domain models.

Covers ResourceLimits → Docker HostConfig translation and Job terminal state logic.
These are pure unit tests — no I/O.
"""

import pytest

from app.core.models import (
    FailureReason,
    Job,
    JobSpec,
    JobStatus,
    ResourceLimits,
)


class TestResourceLimits:
    def test_memory_converts_to_docker_format(self):
        limits = ResourceLimits(memory_mb=512)
        config = limits.to_docker_host_config()
        assert config["mem_limit"] == "512m"

    def test_cpu_quota_half_core(self):
        limits = ResourceLimits(cpu_quota=0.5)
        config = limits.to_docker_host_config()
        # 0.5 * 100_000 = 50_000 microseconds out of 100_000 period = 50% of one core
        assert config["cpu_quota"] == 50_000
        assert config["cpu_period"] == 100_000

    def test_cpu_quota_full_core(self):
        limits = ResourceLimits(cpu_quota=1.0)
        config = limits.to_docker_host_config()
        assert config["cpu_quota"] == 100_000

    def test_default_limits_are_sane(self):
        limits = ResourceLimits()
        assert limits.memory_mb == 256
        assert limits.cpu_quota == 1.0


class TestJobTerminalState:
    def test_success_is_terminal(self):
        job = Job()
        job.status = JobStatus.SUCCESS
        assert job.is_terminal() is True

    def test_failed_is_terminal(self):
        job = Job()
        job.status = JobStatus.FAILED
        assert job.is_terminal() is True

    def test_cancelled_is_terminal(self):
        job = Job()
        job.status = JobStatus.CANCELLED
        assert job.is_terminal() is True

    def test_running_is_not_terminal(self):
        job = Job()
        job.status = JobStatus.RUNNING
        assert job.is_terminal() is False

    def test_pending_is_not_terminal(self):
        job = Job()
        job.status = JobStatus.PENDING
        assert job.is_terminal() is False

    def test_retrying_is_not_terminal(self):
        job = Job()
        job.status = JobStatus.RETRYING
        assert job.is_terminal() is False


class TestJobDefaults:
    def test_job_gets_unique_id(self):
        j1, j2 = Job(), Job()
        assert j1.id != j2.id

    def test_job_starts_pending(self):
        job = Job()
        assert job.status == JobStatus.PENDING

    def test_no_container_id_by_default(self):
        job = Job()
        assert job.container_id is None
