from __future__ import annotations

import logging

from aiogram.types import BufferedInputFile, Message

from pytegbot_bot.services.api_client import ApiClientError, PyTegBotApiClient
from pytegbot_shared.models import ExecutionTaskResponse

logger = logging.getLogger(__name__)

PHOTO_MEDIA_TYPES = {"image/jpeg", "image/png"}


async def send_task_artifacts(
    message: Message,
    api_client: PyTegBotApiClient,
    task: ExecutionTaskResponse,
) -> None:
    for artifact in task.artifacts:
        try:
            payload = await api_client.download_artifact(task.task_id, artifact.artifact_id)
        except ApiClientError as exc:
            logger.warning(
                "Failed to download artifact %s for task %s: %s",
                artifact.artifact_id,
                task.task_id,
                exc,
            )
            continue

        buffered_file = BufferedInputFile(payload, filename=artifact.filename)

        try:
            if artifact.media_type in PHOTO_MEDIA_TYPES:
                await message.reply_photo(photo=buffered_file)
            elif artifact.media_type == "image/gif":
                await message.reply_animation(animation=buffered_file)
            else:
                await message.reply_document(document=buffered_file)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to send artifact %s for task %s: %s",
                artifact.artifact_id,
                task.task_id,
                exc,
            )
