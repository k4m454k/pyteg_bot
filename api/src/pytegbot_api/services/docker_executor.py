from __future__ import annotations

import asyncio
import base64
import io
import json
import shlex
import tarfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import docker
from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container

from pytegbot_api.core.config import ExecutionSettings
from pytegbot_api.services.artifact_store import PendingArtifact
from pytegbot_shared.models import TaskStatus

MANIFEST_FILENAME = ".pytegbot-artifacts.json"
MANIFEST_MAX_BYTES = 65_536
ARTIFACT_READY_MARKER = b"__PYTEGBOT_ARTIFACTS_READY__"
ARTIFACT_ACK_FILENAME = ".pytegbot-artifacts.ack"
ARTIFACT_READY_SCAN_BYTES = 4_096
ARTIFACT_COLLECTION_ATTEMPTS = 20
ARTIFACT_COLLECTION_INTERVAL_SECONDS = 0.1
IMAGE_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
}
IMAGE_MEDIA_TYPES_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


@dataclass(slots=True)
class ExecutionResult:
    status: TaskStatus
    output: str | None = None
    error: str | None = None
    exit_code: int | None = None
    artifacts: list[PendingArtifact] | None = None


@dataclass(slots=True)
class StreamedExecutionResult:
    output: str | None = None
    exit_code: int | None = None
    output_limit_exceeded: bool = False
    artifacts: list[PendingArtifact] | None = None


@dataclass(slots=True)
class ArtifactManifestEntry:
    relative_path: str
    filename: str
    media_type: str
    size_bytes: int


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
        container: Container | None = None
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

            stream_task = asyncio.create_task(
                asyncio.to_thread(self._wait_with_streamed_logs, container),
                name=f"pytegbot-stream-{task_id}",
            )
            done, _ = await asyncio.wait({stream_task}, timeout=timeout_seconds)
            if stream_task not in done:
                handle.timed_out = True
                await asyncio.to_thread(self._kill_container, container)
                streamed_result = await self._await_stream_result(stream_task)
            else:
                streamed_result = stream_task.result()

            logs = streamed_result.output if streamed_result else None
            exit_code = streamed_result.exit_code if streamed_result else None
            artifacts = streamed_result.artifacts if streamed_result else None
            if not artifacts:
                container = await asyncio.to_thread(self._client.containers.get, container.id)
                handle.container = container
                artifacts = await asyncio.to_thread(self._collect_artifacts, container)

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
        except DockerException as exc:
            return ExecutionResult(
                status=TaskStatus.FAILED,
                output=streamed_result.output if streamed_result else None,
                error=f"Docker execution failed: {exc}",
                exit_code=streamed_result.exit_code if streamed_result else None,
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
            environment={
                self._settings.code_env_var: encoded_code,
                self._settings.output_dir_env_var: self._settings.output_dir,
            },
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

    async def _await_stream_result(
        self,
        stream_task: asyncio.Task[StreamedExecutionResult],
    ) -> StreamedExecutionResult | None:
        try:
            return await asyncio.wait_for(asyncio.shield(stream_task), timeout=5)
        except (asyncio.TimeoutError, DockerException):
            return None

    def _wait_with_streamed_logs(self, container: Container) -> StreamedExecutionResult:
        collected = bytearray()
        total_bytes = 0
        output_limit_exceeded = False
        artifacts: list[PendingArtifact] = []
        marker_window = bytearray()
        artifact_signal_seen = False
        stream = None

        try:
            stream = container.logs(stdout=True, stderr=True, stream=True, follow=True)
            for chunk in stream:
                if not chunk:
                    continue

                total_bytes += len(chunk)
                remaining = self._settings.max_output_bytes - len(collected)
                if remaining > 0:
                    collected.extend(chunk[:remaining])

                if not artifacts:
                    marker_window.extend(chunk)
                    if len(marker_window) > ARTIFACT_READY_SCAN_BYTES:
                        del marker_window[:-ARTIFACT_READY_SCAN_BYTES]
                    if ARTIFACT_READY_MARKER in marker_window:
                        artifact_signal_seen = True
                        artifacts = self._collect_artifacts_with_retry(container)
                        if artifacts:
                            self._ack_artifact_pickup(container)

                if total_bytes > self._settings.max_output_bytes:
                    output_limit_exceeded = True
                    self._kill_container(container)
                    break
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()

        wait_result = container.wait()
        return StreamedExecutionResult(
            output=self._decode_output(collected, truncated=output_limit_exceeded),
            exit_code=self._extract_exit_code(wait_result),
            output_limit_exceeded=output_limit_exceeded,
            artifacts=artifacts if artifacts else None,
        )

    def _collect_artifacts(self, container: Container) -> list[PendingArtifact]:
        manifest = self._read_manifest(container)
        if not manifest:
            return self._collect_artifacts_from_output_archive(container)

        artifacts: list[PendingArtifact] = []
        total_bytes = 0
        for raw_entry in manifest:
            if len(artifacts) >= self._settings.max_artifact_count:
                break

            entry = self._parse_manifest_entry(raw_entry)
            if entry is None:
                continue
            if entry.size_bytes > self._settings.max_artifact_bytes_per_file:
                continue
            if total_bytes + entry.size_bytes > self._settings.max_artifact_bytes_total:
                break

            container_path = self._resolve_output_path(entry.relative_path)
            if container_path is None:
                continue

            data = self._read_container_file(
                container,
                container_path,
                max_bytes=self._settings.max_artifact_bytes_per_file,
            )
            if data is None or len(data) != entry.size_bytes:
                continue
            if not self._matches_media_type(entry.media_type, data):
                continue

            total_bytes += len(data)
            artifacts.append(
                PendingArtifact(
                    filename=entry.filename,
                    media_type=entry.media_type,
                    data=data,
                )
            )
        return artifacts or self._collect_artifacts_from_output_archive(container)

    def _collect_artifacts_with_retry(self, container: Container) -> list[PendingArtifact]:
        for attempt in range(ARTIFACT_COLLECTION_ATTEMPTS):
            artifacts = self._collect_artifacts(container)
            if artifacts:
                return artifacts
            if attempt + 1 < ARTIFACT_COLLECTION_ATTEMPTS:
                time.sleep(ARTIFACT_COLLECTION_INTERVAL_SECONDS)
        return []

    def _ack_artifact_pickup(self, container: Container) -> None:
        ack_path = self._resolve_output_path(ARTIFACT_ACK_FILENAME)
        if ack_path is None:
            return

        try:
            container.exec_run(
                ["/bin/sh", "-lc", f"touch {shlex.quote(ack_path)}"],
                stdout=False,
                stderr=False,
            )
        except (APIError, DockerException, NotFound):
            return

    def _read_manifest(self, container: Container) -> list[dict[str, Any]]:
        manifest_path = self._resolve_output_path(MANIFEST_FILENAME)
        if manifest_path is None:
            return []

        raw = self._read_container_file(container, manifest_path, max_bytes=MANIFEST_MAX_BYTES)
        if raw is None:
            return []

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []

        artifacts = payload.get("artifacts")
        return artifacts if isinstance(artifacts, list) else []

    def _parse_manifest_entry(self, raw_entry: Any) -> ArtifactManifestEntry | None:
        if not isinstance(raw_entry, dict):
            return None

        relative_path = raw_entry.get("relative_path")
        filename = raw_entry.get("filename")
        media_type = raw_entry.get("media_type")
        size_bytes = raw_entry.get("size_bytes")

        if not isinstance(relative_path, str) or not relative_path:
            return None
        if not isinstance(filename, str) or not filename:
            return None
        if not isinstance(media_type, str) or not media_type:
            return None
        if not isinstance(size_bytes, int) or size_bytes < 0:
            return None

        resolved_path = self._resolve_output_path(relative_path)
        if resolved_path is None:
            return None

        return ArtifactManifestEntry(
            relative_path=relative_path,
            filename=PurePosixPath(filename).name,
            media_type=media_type,
            size_bytes=size_bytes,
        )

    def _resolve_output_path(self, relative_path: str) -> str | None:
        base = PurePosixPath(self._settings.output_dir)
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            return None
        candidate = base.joinpath(relative)
        if not str(candidate).startswith(str(base)):
            return None
        return str(candidate)

    def _read_container_file(
        self,
        container: Container,
        path: str,
        *,
        max_bytes: int,
    ) -> bytes | None:
        output = self._read_container_file_via_exec(
            container,
            path,
            max_bytes=max_bytes,
        )
        if output is not None:
            return output

        try:
            archive_stream, _ = container.get_archive(path)
        except (APIError, NotFound):
            return None

        archive = self._read_archive_stream(archive_stream, max_bytes=max_bytes + 1_048_576)
        if archive is None:
            return None

        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
                regular_files = [member for member in tar.getmembers() if member.isfile()]
                if len(regular_files) != 1:
                    return None
                member = regular_files[0]
                if member.size > max_bytes:
                    return None
                extracted = tar.extractfile(member)
                if extracted is None:
                    return None
                data = extracted.read(max_bytes + 1)
                if len(data) > max_bytes:
                    return None
                return data
        except tarfile.TarError:
            return None

    def _read_container_file_via_exec(
        self,
        container: Container,
        path: str,
        *,
        max_bytes: int,
    ) -> bytes | None:
        try:
            result = container.exec_run(
                [
                    "python",
                    "-c",
                    (
                        "from pathlib import Path; "
                        "import sys; "
                        "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes())"
                    ),
                    path,
                ],
                stdout=True,
                stderr=False,
            )
        except (APIError, DockerException, NotFound):
            return None

        exit_code = getattr(result, "exit_code", None)
        output = getattr(result, "output", None)
        if exit_code is None and isinstance(result, tuple) and len(result) == 2:
            exit_code, output = result

        if exit_code != 0 or not isinstance(output, (bytes, bytearray)):
            return None
        data = bytes(output)
        if len(data) > max_bytes:
            return None
        return data

    def _collect_artifacts_from_output_archive(self, container: Container) -> list[PendingArtifact]:
        try:
            archive_stream, _ = container.get_archive(self._settings.output_dir)
        except (APIError, NotFound):
            return []

        archive = self._read_archive_stream(
            archive_stream,
            max_bytes=self._settings.max_artifact_bytes_total + 4_194_304,
        )
        if archive is None:
            return []

        artifacts: list[PendingArtifact] = []
        total_bytes = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
                for member in tar.getmembers():
                    if len(artifacts) >= self._settings.max_artifact_count:
                        break
                    if not member.isfile():
                        continue

                    member_path = PurePosixPath(member.name)
                    if member_path.name == MANIFEST_FILENAME:
                        continue

                    media_type = IMAGE_MEDIA_TYPES_BY_SUFFIX.get(member_path.suffix.lower())
                    if media_type is None:
                        continue
                    if member.size > self._settings.max_artifact_bytes_per_file:
                        continue
                    if total_bytes + member.size > self._settings.max_artifact_bytes_total:
                        break

                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    data = extracted.read(self._settings.max_artifact_bytes_per_file + 1)
                    if len(data) > self._settings.max_artifact_bytes_per_file:
                        continue
                    if not self._matches_media_type(media_type, data):
                        continue

                    total_bytes += len(data)
                    artifacts.append(
                        PendingArtifact(
                            filename=member_path.name,
                            media_type=media_type,
                            data=data,
                        )
                    )
        except tarfile.TarError:
            return []

        return artifacts

    @staticmethod
    def _read_archive_stream(
        archive_stream: Any,
        *,
        max_bytes: int,
    ) -> bytes | None:
        archive = bytearray()
        for chunk in archive_stream:
            archive.extend(chunk)
            if len(archive) > max_bytes:
                return None
        return bytes(archive)

    @staticmethod
    def _matches_media_type(media_type: str, data: bytes) -> bool:
        if media_type == "image/webp":
            return (
                len(data) >= 12
                and data.startswith(b"RIFF")
                and data[8:12] == b"WEBP"
            )

        signatures = IMAGE_SIGNATURES.get(media_type)
        if signatures is None:
            return False
        return any(data.startswith(signature) for signature in signatures)

    @staticmethod
    def _decode_output(raw: bytes, *, truncated: bool) -> str | None:
        if not raw:
            return None
        text = raw.decode("utf-8", errors="replace")
        text = text.replace(f"\n{ARTIFACT_READY_MARKER.decode('ascii')}\n", "\n")
        text = text.replace(f"{ARTIFACT_READY_MARKER.decode('ascii')}\n", "")
        text = text.replace(f"\n{ARTIFACT_READY_MARKER.decode('ascii')}", "\n")
        text = text.replace(ARTIFACT_READY_MARKER.decode("ascii"), "")
        if truncated:
            text = f"{text}\n..."
        return text.strip() or None

    @staticmethod
    def _extract_exit_code(wait_result: Any) -> int | None:
        if isinstance(wait_result, dict):
            code = wait_result.get("StatusCode")
            return int(code) if code is not None else None
        return None
