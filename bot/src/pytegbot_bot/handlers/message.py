from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.types import BufferedInputFile, Message

from pytegbot_bot.services.api_client import ApiClientError, PyTegBotApiClient
from pytegbot_bot.services.code_parser import (
    decode_python_source_bytes,
    extract_code_from_command,
    matches_code_command,
    normalize_code,
)
from pytegbot_bot.services.formatter import (
    format_executing_message,
    format_executing_file_message,
    format_execution_message,
    format_execution_file_message,
    format_request_error_message,
    format_request_error_file_message,
)
from pytegbot_shared.models import ExecutionTaskResponse

logger = logging.getLogger(__name__)

PHOTO_MEDIA_TYPES = {"image/jpeg", "image/png"}
MAX_PY_FILE_BYTES = 5 * 1024 * 1024
MEDIA_GROUP_TRACK_SECONDS = 300.0
MULTIPLE_FILES_ERROR = "Please send only one .py file."
INVALID_FILE_ERROR = "Please send a single .py file."
FILE_TOO_LARGE_ERROR = "Python file is too large. The limit is 5 MiB."
FILE_DECODE_ERROR = "Python file must be valid text source."
EMPTY_FILE_ERROR = "Python file is empty."


def build_router(
    *,
    bot_username: str,
    api_client: PyTegBotApiClient,
    poll_interval_seconds: float,
) -> Router:
    router = Router(name="message")
    seen_media_groups: dict[str, float] = {}
    media_group_lock = asyncio.Lock()

    async def execute_submission(
        message: Message,
        *,
        code: str,
        file_name: str | None = None,
    ) -> None:
        is_file_submission = file_name is not None
        text = message.text or message.caption or ""

        pending_message: Message | None = None
        try:
            accepted = await api_client.create_task(code=code, source="message")
            last_rendered = (
                format_executing_file_message(file_name, status=accepted.status)
                if file_name is not None
                else format_executing_message(code, status=accepted.status)
            )
            pending_message = await message.reply(last_rendered)

            async def handle_task_update(task) -> None:
                nonlocal last_rendered
                if task.is_finished:
                    return

                rendered = (
                    format_executing_file_message(file_name, status=task.status)
                    if file_name is not None
                    else format_executing_message(
                        code,
                        status=task.status,
                    )
                )
                if rendered == last_rendered:
                    return

                await pending_message.edit_text(rendered)
                last_rendered = rendered

            task = await api_client.wait_for_terminal(
                accepted.task_id,
                poll_interval_seconds=poll_interval_seconds,
                on_update=handle_task_update,
            )
        except ApiClientError as exc:
            logger.warning("Failed to process message task: %s", exc)
            if pending_message is None:
                await message.reply(
                    format_request_error_file_message(file_name, str(exc))
                    if file_name is not None
                    else format_request_error_message(code, str(exc))
                )
                return
            await pending_message.edit_text(
                format_request_error_file_message(file_name, str(exc))
                if file_name is not None
                else format_request_error_message(code, str(exc))
            )
            return

        await pending_message.edit_text(
            format_execution_file_message(file_name, task)
            if file_name is not None
            else format_execution_message(code, task)
        )
        await send_task_artifacts(message, api_client, task)

    async def remember_media_group(media_group_id: str) -> bool:
        async with media_group_lock:
            now = time.monotonic()
            for tracked_group_id, tracked_at in list(seen_media_groups.items()):
                if now - tracked_at > MEDIA_GROUP_TRACK_SECONDS:
                    seen_media_groups.pop(tracked_group_id, None)

            if media_group_id in seen_media_groups:
                return False

            seen_media_groups[media_group_id] = now
            return True

    @router.message()
    async def handle_python_message(message: Message) -> None:
        if message.document is not None:
            text = message.caption or ""
            is_private = message.chat.type == ChatType.PRIVATE
            if not is_private and not matches_code_command(text, bot_username):
                return

            if message.media_group_id:
                if await remember_media_group(message.media_group_id):
                    await message.reply(MULTIPLE_FILES_ERROR)
                return

            document = message.document
            file_name = (document.file_name or "").strip() or "script.py"
            if not file_name.lower().endswith(".py"):
                await message.reply(INVALID_FILE_ERROR)
                return
            if (document.file_size or 0) > MAX_PY_FILE_BYTES:
                await message.reply(FILE_TOO_LARGE_ERROR)
                return

            payload_io = await message.bot.download(document)
            if payload_io is None:
                await message.reply(FILE_DECODE_ERROR)
                return
            payload = payload_io.getvalue()
            if len(payload) > MAX_PY_FILE_BYTES:
                await message.reply(FILE_TOO_LARGE_ERROR)
                return

            try:
                code = decode_python_source_bytes(payload)
            except (SyntaxError, UnicodeDecodeError, ValueError):
                await message.reply(FILE_DECODE_ERROR)
                return

            if not code.strip():
                await message.reply(EMPTY_FILE_ERROR)
                return

            await execute_submission(
                message,
                code=code,
                file_name=file_name,
            )
            return

        text = message.text or message.caption or ""
        if not text:
            return

        if message.chat.type == ChatType.PRIVATE:
            code = extract_code_from_command(text, bot_username)
            if code is None:
                if text.lstrip().startswith("/"):
                    return
                code = normalize_code(text)
        else:
            code = extract_code_from_command(text, bot_username)
        if not code:
            return

        await execute_submission(message, code=code)

    return router


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
