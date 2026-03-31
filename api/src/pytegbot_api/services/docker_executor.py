from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any

import docker
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container

from pytegbot_api.core.config import ExecutionSettings
from pytegbot_shared.models import TaskStatus


@dataclass(slots=True)
class ExecutionResult:
    status: TaskStatus
    output: str | None = None
    error: str | None = None
    exit_code: int | None = None


@dataclass(slots=True)
class RunningContainerHandle:
    task_id: str
    container: Container | None = None
    cancel_requested: bool = False
    timed_out: bool = False


class DockerCodeExecutor:
    def __init__(self, settings: ExecutionSettings) -> None:
        self._settings = settings
        self._client = docker.DockerClient(base_url=settings.docker_base_url)
        self._lock = asyncio.Lock()
        self._active: dict[str, RunningContainerHandle] = {}

    async def execute(self, task_id: str, code: str, timeout_seconds: int) -> ExecutionResult:
        handle = RunningContainerHandle(task_id=task_id)
        logs: str | None = None
        container: Container | None = None

        async with self._lock:
            self._active[task_id] = handle

        try:
            if handle.cancel_requested:
                return ExecutionResult(
                    status=TaskStatus.CANCELLED,
                    error="Task cancelled before container start.",
                )

            encoded_code = base64.b64encode(code.encode("utf-8")).decode("ascii")
            container = await asyncio.to_thread(self._create_container, task_id, encoded_code)
            handle.container = container

            if handle.cancel_requested:
                return ExecutionResult(
                    status=TaskStatus.CANCELLED,
                    error="Task cancelled before container start.",
                )

            await asyncio.to_thread(container.start)

            if handle.cancel_requested:
                await asyncio.to_thread(self._kill_container, container)

            wait_result = await asyncio.wait_for(
                asyncio.to_thread(container.wait),
                timeout=timeout_seconds,
            )
            logs = await asyncio.to_thread(self._read_logs, container)
            exit_code = self._extract_exit_code(wait_result)

            if handle.timed_out:
                return ExecutionResult(
                    status=TaskStatus.TIMED_OUT,
                    output=logs,
                    error=f"Execution exceeded {timeout_seconds} seconds.",
                    exit_code=exit_code,
                )

            if handle.cancel_requested:
                return ExecutionResult(
                    status=TaskStatus.CANCELLED,
                    output=logs,
                    error="Task cancelled by client.",
                    exit_code=exit_code,
                )

            if exit_code == 0:
                return ExecutionResult(
                    status=TaskStatus.SUCCEEDED,
                    output=logs,
                    exit_code=exit_code,
                )

            return ExecutionResult(
                status=TaskStatus.FAILED,
                output=logs,
                exit_code=exit_code,
            )
        except asyncio.TimeoutError:
            handle.timed_out = True
            if container is not None:
                await asyncio.to_thread(self._kill_container, container)
                logs = await asyncio.to_thread(self._read_logs, container)
            return ExecutionResult(
                status=TaskStatus.TIMED_OUT,
                output=logs,
                error=f"Execution exceeded {timeout_seconds} seconds.",
            )
        except DockerException as exc:
            if container is not None:
                logs = await asyncio.to_thread(self._read_logs, container)
            return ExecutionResult(
                status=TaskStatus.FAILED,
                output=logs,
                error=f"Docker execution failed: {exc}",
            )
        finally:
            if container is not None:
                await asyncio.to_thread(self._remove_container, container)
            async with self._lock:
                self._active.pop(task_id, None)

    async def cancel(self, task_id: str) -> bool:
        async with self._lock:
            handle = self._active.get(task_id)
            if handle is None:
                return False
            handle.cancel_requested = True
            container = handle.container

        if container is not None:
            await asyncio.to_thread(self._kill_container, container)
        return True

    async def close(self) -> None:
        await asyncio.to_thread(self._client.close)

    def _create_container(self, task_id: str, encoded_code: str) -> Container:
        return self._client.containers.create(
            image=self._settings.execution_image,
            detach=True,
            environment={self._settings.code_env_var: encoded_code},
            labels={"pytegbot.task_id": task_id},
            mem_limit=self._settings.memory_limit,
            nano_cpus=self._settings.nano_cpus,
            network_disabled=True,
            read_only=True,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            pids_limit=64,
            tmpfs={"/tmp": "rw,noexec,nosuid,size=64m"},
        )

    def _kill_container(self, container: Container) -> None:
        try:
            container.kill()
        except (APIError, NotFound):
            return

    def _remove_container(self, container: Container) -> None:
        try:
            container.remove(force=True)
        except (APIError, NotFound):
            return

    def _read_logs(self, container: Container) -> str | None:
        try:
            raw = container.logs(stdout=True, stderr=True)
        except (APIError, NotFound):
            return None
        if raw is None:
            return None
        return raw.decode("utf-8", errors="replace").strip() or None

    @staticmethod
    def _extract_exit_code(wait_result: Any) -> int | None:
        if isinstance(wait_result, dict):
            code = wait_result.get("StatusCode")
            return int(code) if code is not None else None
        return None
