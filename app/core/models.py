"""
Domain models for the task runner.

These are plain dataclasses — no ORM coupling. The database layer
translates between these and SQLite rows. Keeping them separate means
we can swap the storage backend without touching business logic.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class JobStatus(str, Enum):
    PENDING = "pending"       # queued, not yet dispatched
    RUNNING = "running"       # container is alive
    SUCCESS = "success"       # exit code 0
    FAILED = "failed"         # non-zero exit or OOM kill
    CANCELLED = "cancelled"   # explicit cancel request
    RETRYING = "retrying"     # waiting for next retry attempt


class FailureReason(str, Enum):
    EXIT_CODE = "non_zero_exit"
    OOM_KILLED = "oom_killed"       # cgroup memory.max breached
    TIMEOUT = "timeout"
    DOCKER_ERROR = "docker_error"   # daemon unreachable, image pull failed, etc.
    CANCELLED = "cancelled"


@dataclass
class ResourceLimits:
    """
    Maps directly to Docker HostConfig fields.

    memory_mb: hard limit enforced by cgroups v2 memory.max
    cpu_quota: fraction of a CPU core (0.5 = 50% of one core)
    """
    memory_mb: int = 256
    cpu_quota: float = 1.0

    def to_docker_host_config(self) -> dict:
        return {
            "mem_limit": f"{self.memory_mb}m",
            # Docker translates cpu_quota/cpu_period into the cgroup cpu.max file.
            # cpu_period is 100_000 microseconds (100ms) by default.
            "cpu_quota": int(self.cpu_quota * 100_000),
            "cpu_period": 100_000,
        }


@dataclass
class JobSpec:
    """What the caller submits. Immutable once accepted."""
    image: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    max_retries: int = 0
    timeout_seconds: Optional[int] = None


@dataclass
class Job:
    """Full job record including runtime state."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    spec: JobSpec = field(default_factory=lambda: JobSpec(image="", command=[]))
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    # set once the container is created
    container_id: Optional[str] = None

    attempt: int = 0            # current attempt number (0-indexed)
    exit_code: Optional[int] = None
    failure_reason: Optional[FailureReason] = None
    error_message: Optional[str] = None

    def is_terminal(self) -> bool:
        return self.status in {
            JobStatus.SUCCESS,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
