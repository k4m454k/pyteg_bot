from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from pytegbot_bot.core.config import MessageSettings
from pytegbot_bot.services.api_client import ApiClientError, PyTegBotApiClient
from pytegbot_bot.services.code_parser import (
    decode_python_source_bytes,
    extract_code_from_command,
    matches_code_command,
    normalize_code,
)
from pytegbot_bot.services.formatter import (
    format_executing_file_message,
    format_executing_message,
    format_execution_file_message,
    format_execution_message,
    format_request_error_file_message,
    format_request_error_message,
)
from pytegbot_bot.services.telegram_artifacts import send_task_artifacts

logger = logging.getLogger(__name__)

router = Router(name="message")

MULTIPLE_FILES_ERROR = "Please send only one .py file."
INVALID_FILE_ERROR = "Please send a single .py file."
FILE_DECODE_ERROR = "Python file must be valid text source."
EMPTY_FILE_ERROR = "Python file is empty."


@dataclass(slots=True)
class MessageHandlerContext:
    bot_username: str
    api_client: PyTegBotApiClient
    poll_interval_seconds: float
    settings: MessageSettings
    seen_media_groups: dict[str, float] = field(default_factory=dict)
    media_group_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_context: MessageHandlerContext | None = None


def configure_router(
    *,
    bot_username: str,
    api_client: PyTegBotApiClient,
    poll_interval_seconds: float,
    settings: MessageSettings,
) -> Router:
    global _context
    _context = MessageHandlerContext(
        bot_username=bot_username,
        api_client=api_client,
        poll_interval_seconds=poll_interval_seconds,
        settings=settings,
    )
    return router


def _get_context() -> MessageHandlerContext:
    if _context is None:
        raise RuntimeError("Message router is not configured.")
    return _context


def _file_too_large_error(max_bytes: int) -> str:
    size_mebibytes = max_bytes / (1024 * 1024)
    if size_mebibytes.is_integer():
        size_text = f"{int(size_mebibytes)} MiB"
    else:
        size_text = f"{size_mebibytes:.1f} MiB"
    return f"Python file is too large. The limit is {size_text}."


async def _execute_submission(
    message: Message,
    *,
    code: str,
    file_name: str | None = None,
) -> None:
    context = _get_context()
    pending_message: Message | None = None

    try:
        accepted = await context.api_client.create_task(code=code, source="message")
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
                else format_executing_message(code, status=task.status)
            )
            if rendered == last_rendered:
                return

            await pending_message.edit_text(rendered)
            last_rendered = rendered

        task = await context.api_client.wait_for_terminal(
            accepted.task_id,
            poll_interval_seconds=context.poll_interval_seconds,
            on_update=handle_task_update,
        )
    except ApiClientError as exc:
        logger.warning("Failed to process message task: %s", exc)
        rendered_error = (
            format_request_error_file_message(file_name, str(exc))
            if file_name is not None
            else format_request_error_message(code, str(exc))
        )
        if pending_message is None:
            await message.reply(rendered_error)
            return
        await pending_message.edit_text(rendered_error)
        return

    await pending_message.edit_text(
        format_execution_file_message(file_name, task)
        if file_name is not None
        else format_execution_message(code, task)
    )
    await send_task_artifacts(message, context.api_client, task)


async def _remember_media_group(media_group_id: str) -> bool:
    context = _get_context()
    async with context.media_group_lock:
        now = time.monotonic()
        for tracked_group_id, tracked_at in list(context.seen_media_groups.items()):
            if now - tracked_at > context.settings.media_group_track_seconds:
                context.seen_media_groups.pop(tracked_group_id, None)

        if media_group_id in context.seen_media_groups:
            return False

        context.seen_media_groups[media_group_id] = now
        return True


async def _handle_python_file(message: Message, *, require_command: bool) -> bool:
    document = message.document
    if document is None:
        return False

    context = _get_context()
    text = message.caption or ""
    if require_command and not matches_code_command(text, context.bot_username):
        return False

    if message.media_group_id:
        if await _remember_media_group(message.media_group_id):
            await message.reply(MULTIPLE_FILES_ERROR)
        return True

    file_name = (document.file_name or "").strip() or "script.py"
    if not file_name.lower().endswith(".py"):
        await message.reply(INVALID_FILE_ERROR)
        return True
    if (document.file_size or 0) > context.settings.max_py_file_bytes:
        await message.reply(_file_too_large_error(context.settings.max_py_file_bytes))
        return True

    payload_io = await message.bot.download(document)
    if payload_io is None:
        await message.reply(FILE_DECODE_ERROR)
        return True
    payload = payload_io.getvalue()
    if len(payload) > context.settings.max_py_file_bytes:
        await message.reply(_file_too_large_error(context.settings.max_py_file_bytes))
        return True

    try:
        code = decode_python_source_bytes(payload)
    except (SyntaxError, UnicodeDecodeError, ValueError):
        await message.reply(FILE_DECODE_ERROR)
        return True

    if not code.strip():
        await message.reply(EMPTY_FILE_ERROR)
        return True

    await _execute_submission(message, code=code, file_name=file_name)
    return True


async def _handle_private_text(message: Message) -> None:
    context = _get_context()
    text = message.text or message.caption or ""
    if not text:
        return

    code = extract_code_from_command(text, context.bot_username)
    if code is None:
        if text.lstrip().startswith("/"):
            return
        code = normalize_code(text)
    if not code:
        return

    await _execute_submission(message, code=code)


async def _handle_group_text(message: Message) -> None:
    context = _get_context()
    text = message.text or message.caption or ""
    if not text:
        return

    code = extract_code_from_command(text, context.bot_username)
    if not code:
        return

    await _execute_submission(message, code=code)


@router.message(F.chat.type == ChatType.PRIVATE)
async def handle_private_message(message: Message) -> None:
    if await _handle_python_file(message, require_command=False):
        return
    await _handle_private_text(message)


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def handle_group_message(message: Message) -> None:
    if await _handle_python_file(message, require_command=True):
        return
    await _handle_group_text(message)
