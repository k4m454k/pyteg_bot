from __future__ import annotations

from dataclasses import dataclass

from docker.models.containers import Container

from pytegbot_api.services.artifact_store import PendingArtifact
from pytegbot_shared.models import TaskStatus


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
