from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pytegbot_shared.models import TaskArtifactSummary


@dataclass(slots=True)
class PendingArtifact:
    filename: str
    media_type: str
    data: bytes


@dataclass(slots=True)
class StoredArtifact:
    artifact_id: str
    task_id: str
    filename: str
    media_type: str
    size_bytes: int
    path: Path

    def to_summary(self) -> TaskArtifactSummary:
        return TaskArtifactSummary(
            artifact_id=self.artifact_id,
            filename=self.filename,
            media_type=self.media_type,
            size_bytes=self.size_bytes,
        )


class ArtifactStore:
    def __init__(self, *, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._lock = asyncio.Lock()
        self._artifacts: dict[str, dict[str, StoredArtifact]] = {}

    async def ensure_base_dir(self) -> None:
        await asyncio.to_thread(self._base_dir.mkdir, parents=True, exist_ok=True)

    async def save_task_artifacts(
        self,
        task_id: str,
        artifacts: list[PendingArtifact],
    ) -> list[TaskArtifactSummary]:
        async with self._lock:
            await asyncio.to_thread(self._delete_task_artifacts_sync, task_id)
            stored = await asyncio.to_thread(self._save_task_artifacts_sync, task_id, artifacts)
            self._artifacts[task_id] = {artifact.artifact_id: artifact for artifact in stored}
            return [artifact.to_summary() for artifact in stored]

    async def get_artifact(self, task_id: str, artifact_id: str) -> StoredArtifact | None:
        async with self._lock:
            task_artifacts = self._artifacts.get(task_id)
            if task_artifacts is None:
                return None
            artifact = task_artifacts.get(artifact_id)
            if artifact is None or not artifact.path.exists():
                return None
            return StoredArtifact(
                artifact_id=artifact.artifact_id,
                task_id=artifact.task_id,
                filename=artifact.filename,
                media_type=artifact.media_type,
                size_bytes=artifact.size_bytes,
                path=artifact.path,
            )

    async def delete_task_artifacts(self, task_id: str) -> None:
        async with self._lock:
            self._artifacts.pop(task_id, None)
            await asyncio.to_thread(self._delete_task_artifacts_sync, task_id)

    def _save_task_artifacts_sync(
        self,
        task_id: str,
        artifacts: list[PendingArtifact],
    ) -> list[StoredArtifact]:
        if not artifacts:
            return []

        task_dir = self._base_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        stored: list[StoredArtifact] = []
        for artifact in artifacts:
            artifact_id = uuid4().hex
            filename = self._sanitize_filename(artifact.filename)
            path = task_dir / f"{artifact_id}-{filename}"
            path.write_bytes(artifact.data)
            stored.append(
                StoredArtifact(
                    artifact_id=artifact_id,
                    task_id=task_id,
                    filename=filename,
                    media_type=artifact.media_type,
                    size_bytes=len(artifact.data),
                    path=path,
                )
            )
        return stored

    def _delete_task_artifacts_sync(self, task_id: str) -> None:
        task_dir = self._base_dir / task_id
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        candidate = Path(filename).name.strip() or "artifact"
        return candidate.replace("/", "_")
