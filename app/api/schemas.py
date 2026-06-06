"""
API schemas: Pydantic models for request validation and response serialisation.

Keeping these separate from the domain models (app/core/models.py) is
deliberate. The domain models are pure Python dataclasses — no Pydantic
dependency. This means the scheduler and retry engine can be tested without
any HTTP or serialisation code in scope.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class ResourceLimitsSchema(BaseModel):
    memory_mb: int = Field(default=256, ge=32, le=8192, description="Memory limit in MB (cgroups v2 memory.max)")
    cpu_quota: float = Field(default=1.0, gt=0.0, le=16.0, description="CPU cores fraction (0.5 = half a core)")


class SubmitJobRequest(BaseModel):
    image: str = Field(..., min_length=1, description="Docker image to run, e.g. 'python:3.12-slim'")
    command: list[str] = Field(..., min_length=1, description="Command and arguments, e.g. ['python', '-c', 'print(1)']")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables")
    limits: ResourceLimitsSchema = Field(default_factory=ResourceLimitsSchema)
    max_retries: int = Field(default=0, ge=0, le=10)
    timeout_seconds: Optional[int] = Field(default=None, ge=1, le=3600)

    @field_validator("image")
    @classmethod
    def image_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("image must not be blank")
        return v.strip()


class JobResponse(BaseModel):
    id: str
    status: str
    image: str
    command: list[str]
    attempt: int
    exit_code: Optional[int]
    failure_reason: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
