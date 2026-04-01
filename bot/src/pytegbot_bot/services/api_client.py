from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from pytegbot_bot.core.config import ApiClientSettings
from pytegbot_shared.models import (
    ExecutionTaskAccepted,
    ExecutionTaskResponse,
    TERMINAL_STATUSES,
)


class ApiClientError(RuntimeError):
    pass


TaskUpdateCallback = Callable[[ExecutionTaskResponse], Awaitable[None]]


class PyTegBotApiClient:
    def __init__(self, settings: ApiClientSettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=settings.request_timeout_seconds,
        )

    async def create_task(
        self,
        *,
        code: str,
        source: str,
        timeout_seconds: int | None = None,
    ) -> ExecutionTaskAccepted:
        payload = {"code": code, "source": source}
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
        response = await self._request("POST", "/v1/tasks", json=payload)
        return ExecutionTaskAccepted.model_validate(response.json())

    async def get_task(self, task_id: str) -> ExecutionTaskResponse:
        response = await self._request("GET", f"/v1/tasks/{task_id}")
        return ExecutionTaskResponse.model_validate(response.json())

    async def cancel_task(self, task_id: str) -> ExecutionTaskResponse | None:
        try:
            response = await self._request("POST", f"/v1/tasks/{task_id}/cancel")
        except ApiClientError as exc:
            if "404" in str(exc):
                return None
            raise
        return ExecutionTaskResponse.model_validate(response.json())

    async def download_artifact(self, task_id: str, artifact_id: str) -> bytes:
        response = await self._request("GET", f"/v1/tasks/{task_id}/artifacts/{artifact_id}")
        return response.content

    async def wait_for_terminal(
        self,
        task_id: str,
        *,
        poll_interval_seconds: float,
        on_update: TaskUpdateCallback | None = None,
    ) -> ExecutionTaskResponse:
        last_snapshot: tuple | None = None
        while True:
            task = await self.get_task(task_id)
            snapshot = (
                task.status,
                task.started_at,
                task.finished_at,
                task.exit_code,
                task.cancel_requested,
                task.error,
                task.output,
            )
            if on_update is not None and snapshot != last_snapshot:
                await on_update(task)
                last_snapshot = snapshot
            if task.status in TERMINAL_STATUSES:
                return task
            await asyncio.sleep(poll_interval_seconds)

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._settings.auth_token}"
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ApiClientError(
                f"API request failed with {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ApiClientError(f"API request failed: {exc}") from exc
        return response
