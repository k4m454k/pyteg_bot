from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


TERMINAL_STATUSES = frozenset(
    {
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
    }
)


class ExecutionTaskCreateRequest(BaseModel):
    code: str = Field(min_length=1, max_length=40000)
    source: Literal["message", "inline", "api"] = "api"
    timeout_seconds: int | None = Field(default=None, ge=1, le=300)


class ExecutionTaskAccepted(BaseModel):
    task_id: str
    status: TaskStatus
    timeout_seconds: int


class ExecutionTaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    source: str
    timeout_seconds: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    output: str | None = None
    error: str | None = None
    cancel_requested: bool = False

    @property
    def is_finished(self) -> bool:
        return self.status in TERMINAL_STATUSES


class HealthResponse(BaseModel):
    status: str
    queue_size: int
    running_tasks: int
