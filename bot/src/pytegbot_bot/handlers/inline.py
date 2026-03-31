from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, ChosenInlineResult, InlineQuery

from pytegbot_bot.services.formatter import INLINE_STATUS_CALLBACK_PREFIX
from pytegbot_bot.services.inline_coordinator import InlineExecutionCoordinator


def build_router(*, coordinator: InlineExecutionCoordinator) -> Router:
    router = Router(name="inline")

    @router.inline_query()
    async def handle_inline_query(inline_query: InlineQuery) -> None:
        await coordinator.process(inline_query)

    @router.chosen_inline_result()
    async def handle_chosen_inline_result(chosen_result: ChosenInlineResult) -> None:
        await coordinator.handle_chosen_result(chosen_result)

    @router.callback_query(F.data.startswith(INLINE_STATUS_CALLBACK_PREFIX))
    async def handle_status_callback(callback_query: CallbackQuery) -> None:
        await coordinator.handle_status_callback(callback_query)

    return router
