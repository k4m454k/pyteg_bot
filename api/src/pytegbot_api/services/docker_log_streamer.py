from __future__ import annotations

from contextlib import suppress
from typing import Any

from docker.models.containers import Container

from pytegbot_api.core.config import ExecutionSettings
from pytegbot_api.services.docker_artifact_collector import (
    ARTIFACT_READY_MARKER,
    DockerArtifactCollector,
)
from pytegbot_api.services.docker_container_runtime import DockerContainerRuntime
from pytegbot_api.services.docker_execution_models import StreamedExecutionResult

ARTIFACT_READY_SCAN_BYTES = 4_096


class DockerLogStreamer:
    def __init__(
        self,
        settings: ExecutionSettings,
        runtime: DockerContainerRuntime,
        artifact_collector: DockerArtifactCollector,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._artifact_collector = artifact_collector

    def wait_with_streamed_logs(self, container: Container) -> StreamedExecutionResult:
        collected = bytearray()
        total_bytes = 0
        output_limit_exceeded = False
        artifacts = []
        marker_window = bytearray()
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
                        artifacts = self._artifact_collector.collect_with_retry(container)
                        if artifacts:
                            self._artifact_collector.ack_pickup(container)

                if total_bytes > self._settings.max_output_bytes:
                    output_limit_exceeded = True
                    self._runtime.kill_container(container)
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
            artifacts=artifacts or None,
        )

    @staticmethod
    def _decode_output(raw: bytes, *, truncated: bool) -> str | None:
        if not raw:
            return None
        marker = ARTIFACT_READY_MARKER.decode("ascii")
        text = raw.decode("utf-8", errors="replace")
        text = text.replace(f"\n{marker}\n", "\n")
        text = text.replace(f"{marker}\n", "")
        text = text.replace(f"\n{marker}", "\n")
        text = text.replace(marker, "")
        if truncated:
            text = f"{text}\n..."
        return text.strip() or None

    @staticmethod
    def _extract_exit_code(wait_result: Any) -> int | None:
        if isinstance(wait_result, dict):
            code = wait_result.get("StatusCode")
            return int(code) if code is not None else None
        return None
