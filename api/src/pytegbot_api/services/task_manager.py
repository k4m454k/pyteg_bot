from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import cast

from pytegbot_api.core.config import ExecutionSettings
from pytegbot_api.services.docker_executor import DockerCodeExecutor
from pytegbot_api.services.task_store import InMemoryTaskStore
from pytegbot_shared.models import ExecutionTaskResponse, HealthResponse, TaskStatus


class ExecutionTaskManager:
    def __init__(
        self,
        *,
        settings: ExecutionSettings,
        store: InMemoryTaskStore,
        executor: DockerCodeExecutor,
    ) -> None:
        self._settings = settings
        self._store = store
        self._executor = executor
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._cleanup_task: asyncio.Task[None] | None = None
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker_loop(index), name=f"pytegbot-worker-{index}")
            for index in range(self._settings.max_concurrent_tasks)
        ]
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="pytegbot-cleanup",
        )

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False

        running_task_ids = await self._store.running_task_ids()
        for task_id in running_task_ids:
            await self.cancel_task(task_id)

        for _ in self._workers:
            await self._queue.put(None)

        with suppress(asyncio.CancelledError):
            await asyncio.gather(*self._workers, return_exceptions=True)

        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._cleanup_task

        self._workers.clear()
        self._cleanup_task = None
        await self._executor.close()

    async def create_task(
        self,
        *,
        code: str,
        source: str,
        timeout_seconds: int | None = None,
    ) -> ExecutionTaskResponse:
        effective_timeout_seconds = min(
            timeout_seconds or self._settings.max_timeout_seconds,
            self._settings.max_timeout_seconds,
        )
        task = await self._store.create_task(
            code=code,
            source=source,
            timeout_seconds=effective_timeout_seconds,
        )
        await self._queue.put(task.task_id)
        return task

    async def get_task(self, task_id: str) -> ExecutionTaskResponse | None:
        return await self._store.get_public_task(task_id)

    async def cancel_task(self, task_id: str) -> ExecutionTaskResponse | None:
        task = await self._store.request_cancel(task_id)
        if task is None:
            return None
        if task.status == TaskStatus.RUNNING:
            await self._executor.cancel(task_id)
        return task

    async def health(self) -> HealthResponse:
        return HealthResponse(
            status="ok",
            queue_size=self._queue.qsize(),
            running_tasks=await self._store.running_count(),
        )

    async def _worker_loop(self, worker_index: int) -> None:
        _ = worker_index
        while True:
            task_id = await self._queue.get()
            try:
                if task_id is None:
                    return
                await self._process_task(cast(str, task_id))
            finally:
                self._queue.task_done()

    async def _process_task(self, task_id: str) -> None:
        record = await self._store.mark_running(task_id)
        if record is None:
            return

        result = await self._executor.execute(
            task_id=task_id,
            code=record.code,
            timeout_seconds=record.timeout_seconds,
        )
        await self._store.apply_result(
            task_id,
            status=result.status,
            output=result.output,
            error=result.error,
            exit_code=result.exit_code,
        )

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._settings.cleanup_interval_seconds)
            await self._store.cleanup_expired()
