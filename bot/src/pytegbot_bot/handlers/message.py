from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from pytegbot_bot.services.api_client import ApiClientError, PyTegBotApiClient
from pytegbot_bot.services.code_parser import (
    extract_code_from_command,
    normalize_code,
)
from pytegbot_bot.services.formatter import (
    format_executing_message,
    format_execution_message,
    format_request_error_message,
)

logger = logging.getLogger(__name__)


def build_router(
    *,
    bot_username: str,
    api_client: PyTegBotApiClient,
    poll_interval_seconds: float,
) -> Router:
    router = Router(name="message")

    @router.message(F.text | F.caption)
    async def handle_python_message(message: Message) -> None:
        text = message.text or message.caption or ""
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

        pending_message: Message | None = None
        try:
            accepted = await api_client.create_task(code=code, source="message")
            last_rendered = format_executing_message(code, status=accepted.status)
            pending_message = await message.reply(last_rendered)

            async def handle_task_update(task) -> None:
                nonlocal last_rendered
                if task.is_finished:
                    return

                rendered = format_executing_message(
                    code,
                    status=task.status,
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
                await message.reply(format_request_error_message(code, str(exc)))
                return
            await pending_message.edit_text(format_request_error_message(code, str(exc)))
            return

        await pending_message.edit_text(format_execution_message(code, task))

    return router
