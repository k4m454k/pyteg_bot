from __future__ import annotations

import io
import json
import shlex
import tarfile
import time
from pathlib import PurePosixPath
from typing import Any

from docker.errors import APIError, DockerException, NotFound
from docker.models.containers import Container

from pytegbot_api.core.config import ExecutionSettings
from pytegbot_api.services.artifact_store import PendingArtifact
from pytegbot_api.services.docker_execution_models import ArtifactManifestEntry

MANIFEST_FILENAME = ".pytegbot-artifacts.json"
MANIFEST_MAX_BYTES = 65_536
ARTIFACT_READY_MARKER = b"__PYTEGBOT_ARTIFACTS_READY__"
ARTIFACT_ACK_FILENAME = ".pytegbot-artifacts.ack"
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


class DockerArtifactCollector:
    def __init__(self, settings: ExecutionSettings) -> None:
        self._settings = settings

    def collect(self, container: Container) -> list[PendingArtifact]:
        manifest = self._read_manifest(container)
        if not manifest:
            return self._collect_from_output_archive(container)

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
        return artifacts or self._collect_from_output_archive(container)

    def collect_with_retry(self, container: Container) -> list[PendingArtifact]:
        for attempt in range(ARTIFACT_COLLECTION_ATTEMPTS):
            artifacts = self.collect(container)
            if artifacts:
                return artifacts
            if attempt + 1 < ARTIFACT_COLLECTION_ATTEMPTS:
                time.sleep(ARTIFACT_COLLECTION_INTERVAL_SECONDS)
        return []

    def ack_pickup(self, container: Container) -> None:
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
        output = self._read_container_file_via_exec(container, path, max_bytes=max_bytes)
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

    @staticmethod
    def _read_container_file_via_exec(
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

    def _collect_from_output_archive(self, container: Container) -> list[PendingArtifact]:
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
