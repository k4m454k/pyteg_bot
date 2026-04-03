from __future__ import annotations

import asyncio
import base64

from docker.errors import DockerException

from pytegbot_api.core.config import ExecutionSettings
from pytegbot_api.services.docker_artifact_collector import DockerArtifactCollector
from pytegbot_api.services.docker_container_runtime import DockerContainerRuntime
from pytegbot_api.services.docker_execution_models import (
    ExecutionResult,
    RunningContainerHandle,
    StreamedExecutionResult,
)
from pytegbot_api.services.docker_log_streamer import DockerLogStreamer
from pytegbot_shared.models import TaskStatus


class DockerCodeExecutor:
    def __init__(self, settings: ExecutionSettings) -> None:
        self._settings = settings
        self._runtime = DockerContainerRuntime(settings)
        self._artifact_collector = DockerArtifactCollector(settings)
        self._log_streamer = DockerLogStreamer(settings, self._runtime, self._artifact_collector)
        self._lock = asyncio.Lock()
        self._active: dict[str, RunningContainerHandle] = {}

    async def execute(self, task_id: str, code: str, timeout_seconds: int) -> ExecutionResult:
        handle = RunningContainerHandle(task_id=task_id)
        container = None
        streamed_result: StreamedExecutionResult | None = None
        stream_task: asyncio.Task[StreamedExecutionResult] | None = None

        async with self._lock:
            self._active[task_id] = handle

        try:
            if handle.cancel_requested:
                return ExecutionResult(
                    status=TaskStatus.CANCELLED,
                    error="Task cancelled before container start.",
                )

            encoded_code, upload_via_file = self._prepare_code_transport(code)
            container = await asyncio.to_thread(
                self._runtime.create_container,
                task_id,
                encoded_code=encoded_code,
                upload_via_file=upload_via_file,
            )
            handle.container = container

            if handle.cancel_requested:
                return ExecutionResult(
                    status=TaskStatus.CANCELLED,
                    error="Task cancelled before container start.",
                )

            await asyncio.to_thread(container.start)
            if handle.cancel_requested:
                await asyncio.to_thread(self._runtime.kill_container, container)
                return ExecutionResult(
                    status=TaskStatus.CANCELLED,
                    error="Task cancelled before code upload.",
                )

            if upload_via_file:
                await self._upload_code(container, code)

            if handle.cancel_requested:
                await asyncio.to_thread(self._runtime.kill_container, container)

            stream_task = asyncio.create_task(
                asyncio.to_thread(self._log_streamer.wait_with_streamed_logs, container),
                name=f"pytegbot-stream-{task_id}",
            )
            done, _ = await asyncio.wait({stream_task}, timeout=timeout_seconds)
            if stream_task not in done:
                handle.timed_out = True
                await asyncio.to_thread(self._runtime.kill_container, container)
                streamed_result = await self._await_stream_result(stream_task)
            else:
                streamed_result = stream_task.result()

            return await self._build_execution_result(
                handle,
                container_id=container.id,
                timeout_seconds=timeout_seconds,
                streamed_result=streamed_result,
            )
        except DockerException as exc:
            return ExecutionResult(
                status=TaskStatus.FAILED,
                output=streamed_result.output if streamed_result else None,
                error=f"Docker execution failed: {exc}",
                exit_code=streamed_result.exit_code if streamed_result else None,
            )
        finally:
            if container is not None:
                await asyncio.to_thread(self._runtime.remove_container, container)
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
            await asyncio.to_thread(self._runtime.kill_container, container)
        return True

    async def close(self) -> None:
        await asyncio.to_thread(self._runtime.close)

    def _prepare_code_transport(self, code: str) -> tuple[str | None, bool]:
        code_bytes = code.encode("utf-8")
        upload_via_file = len(code_bytes) > self._settings.max_env_code_bytes
        if upload_via_file:
            return None, True
        return base64.b64encode(code_bytes).decode("ascii"), False

    async def _upload_code(self, container, code: str) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._runtime.upload_code_file, container, code),
                timeout=self._settings.code_upload_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            await asyncio.to_thread(self._runtime.kill_container, container)
            raise DockerException(
                f"Code upload exceeded {self._settings.code_upload_timeout_seconds} seconds."
            ) from exc

    async def _build_execution_result(
        self,
        handle: RunningContainerHandle,
        *,
        container_id: str,
        timeout_seconds: int,
        streamed_result: StreamedExecutionResult | None,
    ) -> ExecutionResult:
        logs = streamed_result.output if streamed_result else None
        exit_code = streamed_result.exit_code if streamed_result else None
        artifacts = streamed_result.artifacts if streamed_result else None

        if not artifacts:
            container = await asyncio.to_thread(self._runtime.get_container, container_id)
            handle.container = container
            artifacts = await asyncio.to_thread(self._artifact_collector.collect, container)

        if handle.timed_out:
            return ExecutionResult(
                status=TaskStatus.TIMED_OUT,
                output=logs,
                error=f"Execution exceeded {timeout_seconds} seconds.",
                exit_code=exit_code,
                artifacts=artifacts,
            )

        if streamed_result and streamed_result.output_limit_exceeded:
            return ExecutionResult(
                status=TaskStatus.FAILED,
                output=logs,
                error=(
                    "Execution produced too much output and was stopped "
                    f"after {self._settings.max_output_bytes} bytes."
                ),
                exit_code=exit_code,
                artifacts=artifacts,
            )

        if handle.cancel_requested:
            return ExecutionResult(
                status=TaskStatus.CANCELLED,
                output=logs,
                error="Task cancelled by client.",
                exit_code=exit_code,
                artifacts=artifacts,
            )

        if exit_code == 0:
            return ExecutionResult(
                status=TaskStatus.SUCCEEDED,
                output=logs,
                exit_code=exit_code,
                artifacts=artifacts,
            )

        return ExecutionResult(
            status=TaskStatus.FAILED,
            output=logs,
            exit_code=exit_code,
            artifacts=artifacts,
        )

    async def _await_stream_result(
        self,
        stream_task: asyncio.Task[StreamedExecutionResult],
    ) -> StreamedExecutionResult | None:
        try:
            return await asyncio.wait_for(asyncio.shield(stream_task), timeout=5)
        except (asyncio.TimeoutError, DockerException):
            return None
