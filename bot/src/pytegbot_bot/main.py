from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from pytegbot_bot.core.config import get_settings
from pytegbot_bot.handlers.inline import build_router as build_inline_router
from pytegbot_bot.handlers.message import configure_router as configure_message_router
from pytegbot_bot.services.api_client import PyTegBotApiClient
from pytegbot_bot.services.inline_coordinator import InlineExecutionCoordinator


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    bot = Bot(
        token=settings.telegram.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    api_client = PyTegBotApiClient(settings.api)

    me = await bot.get_me()
    if not me.username:
        raise RuntimeError("Telegram bot must have a public username for mentions and inline mode.")

    await bot.set_my_commands(
        [
            BotCommand(
                command="code",
                description="Execute Python code from the message body.",
            )
        ]
    )

    inline_coordinator = InlineExecutionCoordinator(
        bot=bot,
        api_client=api_client,
        poll_interval_seconds=settings.api.poll_interval_seconds,
        debounce_seconds=settings.inline.debounce_seconds,
        cache_time_seconds=settings.inline.cache_time_seconds,
        execution_timeout_seconds=settings.inline.execution_timeout_seconds,
    )

    dispatcher = Dispatcher()
    dispatcher.include_router(
        configure_message_router(
            bot_username=me.username,
            api_client=api_client,
            poll_interval_seconds=settings.api.poll_interval_seconds,
            settings=settings.message,
        )
    )
    dispatcher.include_router(build_inline_router(coordinator=inline_coordinator))
    allowed_updates = dispatcher.resolve_used_update_types()
    for required_update in ("chosen_inline_result", "callback_query"):
        if required_update not in allowed_updates:
            allowed_updates.append(required_update)

    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=allowed_updates,
        )
    finally:
        await api_client.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())
