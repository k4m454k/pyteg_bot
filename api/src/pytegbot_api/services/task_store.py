from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pytegbot_shared.models import (
    ExecutionTaskResponse,
    TaskArtifactSummary,
    TaskStatus,
    TERMINAL_STATUSES,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class TaskRecord:
    task_id: str
    code: str
    source: str
    timeout_seconds: int
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    output: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    artifacts: list[TaskArtifactSummary] | None = None

    def to_response(self) -> ExecutionTaskResponse:
        return ExecutionTaskResponse(
            task_id=self.task_id,
            status=self.status,
            source=self.source,
            timeout_seconds=self.timeout_seconds,
            created_at=self.created_at,
            updated_at=self.updated_at,
            expires_at=self.expires_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            exit_code=self.exit_code,
            output=self.output,
            error=self.error,
            cancel_requested=self.cancel_requested,
            artifacts=list(self.artifacts or []),
        )


class InMemoryTaskStore:
    def __init__(self, *, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = asyncio.Lock()
        self._tasks: dict[str, TaskRecord] = {}

    async def create_task(
        self,
        *,
        code: str,
        source: str,
        timeout_seconds: int,
    ) -> ExecutionTaskResponse:
        now = utcnow()
        record = TaskRecord(
            task_id=uuid4().hex,
            code=code,
            source=source,
            timeout_seconds=timeout_seconds,
            status=TaskStatus.QUEUED,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
        )
        async with self._lock:
            self._tasks[record.task_id] = record
        return record.to_response()

    async def get_record(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            record = self._tasks.get(task_id)
            return deepcopy(record) if record else None

    async def get_public_task(self, task_id: str) -> ExecutionTaskResponse | None:
        record = await self.get_record(task_id)
        return record.to_response() if record else None

    async def mark_running(self, task_id: str) -> TaskRecord | None:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            if record.status != TaskStatus.QUEUED or record.cancel_requested:
                return None
            now = utcnow()
            record.status = TaskStatus.RUNNING
            record.started_at = now
            record.updated_at = now
            return deepcopy(record)

    async def request_cancel(self, task_id: str) -> ExecutionTaskResponse | None:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None

            now = utcnow()
            if record.status == TaskStatus.QUEUED:
                record.status = TaskStatus.CANCELLED
                record.cancel_requested = True
                record.finished_at = now
                record.updated_at = now
                record.error = record.error or "Task cancelled before execution."
            elif record.status == TaskStatus.RUNNING:
                record.cancel_requested = True
                record.updated_at = now

            return deepcopy(record).to_response()

    async def apply_result(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        output: str | None,
        error: str | None,
        exit_code: int | None,
        artifacts: list[TaskArtifactSummary] | None = None,
    ) -> ExecutionTaskResponse | None:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            if record.status in TERMINAL_STATUSES and record.status != TaskStatus.RUNNING:
                return deepcopy(record).to_response()

            now = utcnow()
            record.status = status
            if record.cancel_requested and status == TaskStatus.FAILED:
                record.status = TaskStatus.CANCELLED
                error = error or "Task cancelled by client."
            record.output = output
            record.error = error
            record.exit_code = exit_code
            record.artifacts = list(artifacts or [])
            record.finished_at = now
            record.updated_at = now
            return deepcopy(record).to_response()

    async def cleanup_expired(self) -> list[str]:
        now = utcnow()
        async with self._lock:
            expired_task_ids = [
                task_id
                for task_id, record in self._tasks.items()
                if record.expires_at <= now
            ]
            for task_id in expired_task_ids:
                self._tasks.pop(task_id, None)
            return expired_task_ids

    async def running_task_ids(self) -> list[str]:
        async with self._lock:
            return [
                task_id
                for task_id, record in self._tasks.items()
                if record.status == TaskStatus.RUNNING
            ]

    async def running_count(self) -> int:
        async with self._lock:
            return sum(1 for record in self._tasks.values() if record.status == TaskStatus.RUNNING)
