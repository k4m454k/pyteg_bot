from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, ChosenInlineResult, InlineKeyboardMarkup, InlineQuery

from pytegbot_bot.services.api_client import ApiClientError, PyTegBotApiClient
from pytegbot_bot.services.code_parser import extract_code_from_inline_query
from pytegbot_bot.services.formatter import (
    format_inline_execution_message,
    format_inline_executing_message,
    format_inline_request_error_message,
    inline_executing_result,
    inline_hint_result,
    inline_result_from_task,
    parse_inline_status_task_id,
    inline_status_reply_markup,
)
from pytegbot_shared.models import TERMINAL_STATUSES

logger = logging.getLogger(__name__)
INLINE_DIRECT_RESULT_WAIT_SECONDS = 7.0
INLINE_DIRECT_RESULT_POLL_SECONDS = 0.5


@dataclass(slots=True)
class InlineSession:
    revision: int = 0
    active_task_id: str | None = None


@dataclass(slots=True)
class InlinePendingResult:
    result_id: str
    user_id: int
    revision: int
    task_id: str
    code: str
    inline_message_id: str | None = None
    final_message_text: str | None = None
    edit_applied: bool = False


class InlineExecutionCoordinator:
    def __init__(
        self,
        *,
        bot: Bot,
        api_client: PyTegBotApiClient,
        poll_interval_seconds: float,
        debounce_seconds: float,
        cache_time_seconds: int,
        execution_timeout_seconds: int,
    ) -> None:
        self._bot = bot
        self._api_client = api_client
        self._poll_interval_seconds = poll_interval_seconds
        self._debounce_seconds = debounce_seconds
        self._cache_time_seconds = cache_time_seconds
        self._execution_timeout_seconds = execution_timeout_seconds
        self._lock = asyncio.Lock()
        self._sessions: dict[int, InlineSession] = {}
        self._pending_results: dict[str, InlinePendingResult] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def process(self, query: InlineQuery) -> None:
        revision, previous_task_id = await self._advance_revision(query.from_user.id)
        if previous_task_id:
            await self._safe_cancel(previous_task_id)

        await asyncio.sleep(self._debounce_seconds)
        if not await self._is_current(query.from_user.id, revision):
            await self._safe_answer_empty(query)
            return

        code = extract_code_from_inline_query(query.query)
        if not code:
            await self._answer_with_hint(
                query,
                "Type Python code or paste a fenced block like ```python``` or ```py```.",
            )
            await self._clear_if_current(query.from_user.id, revision)
            return

        try:
            accepted = await self._api_client.create_task(
                code=code,
                source="inline",
                timeout_seconds=self._execution_timeout_seconds,
            )
        except ApiClientError as exc:
            await self._answer_with_hint(query, str(exc))
            await self._clear_if_current(query.from_user.id, revision)
            return

        pending = InlinePendingResult(
            result_id=accepted.task_id,
            user_id=query.from_user.id,
            revision=revision,
            task_id=accepted.task_id,
            code=code,
        )

        if not await self._set_active_task(query.from_user.id, revision, accepted.task_id):
            await self._safe_cancel(accepted.task_id)
            await self._safe_answer_empty(query)
            return

        try:
            task = await asyncio.wait_for(
                self._api_client.wait_for_terminal(
                    accepted.task_id,
                    poll_interval_seconds=min(
                        self._poll_interval_seconds,
                        INLINE_DIRECT_RESULT_POLL_SECONDS,
                    ),
                ),
                timeout=INLINE_DIRECT_RESULT_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            task = None
        except ApiClientError as exc:
            await self._clear_active_task_if_current(query.from_user.id, revision, accepted.task_id)
            if await self._is_current(query.from_user.id, revision):
                await self._answer_with_hint(query, str(exc))
            else:
                await self._safe_answer_empty(query)
            return

        if task is not None:
            await self._clear_active_task_if_current(query.from_user.id, revision, accepted.task_id)
            if not await self._is_current(query.from_user.id, revision):
                await self._safe_answer_empty(query)
                return
            await self._safe_answer_results(query, [inline_result_from_task(task, code)])
            return

        preview_task = None
        try:
            preview_task = await self._api_client.get_task(accepted.task_id)
        except ApiClientError:
            preview_status = accepted.status
        else:
            preview_status = preview_task.status

        if not await self._is_current(query.from_user.id, revision):
            await self._safe_cancel(accepted.task_id)
            await self._safe_answer_empty(query)
            return

        if preview_task is not None and preview_task.status in TERMINAL_STATUSES:
            await self._clear_active_task_if_current(query.from_user.id, revision, accepted.task_id)
            await self._safe_answer_results(query, [inline_result_from_task(preview_task, code)])
            return

        async with self._lock:
            self._pending_results[pending.result_id] = pending

        self._track_background_task(
            self._resolve_pending_result(
                pending=pending,
                user_id=query.from_user.id,
                revision=revision,
            )
        )
        await self._safe_answer_results(
            query,
            [inline_executing_result(pending.result_id, status=preview_status)],
        )

    async def handle_chosen_result(self, chosen_result: ChosenInlineResult) -> None:
        logger.info(
            "Chosen inline result received: result_id=%s inline_message_id_present=%s",
            chosen_result.result_id,
            bool(chosen_result.inline_message_id),
        )
        if not chosen_result.inline_message_id:
            logger.warning(
                "Chosen inline result %s has no inline_message_id; cannot edit message.",
                chosen_result.result_id,
            )
            return

        pending = await self._get_pending_result(chosen_result.result_id)
        if pending is None:
            return

        pending.inline_message_id = chosen_result.inline_message_id
        await self._store_pending_result(pending)
        await self._refresh_inline_progress(pending.task_id, chosen_result.inline_message_id)
        await self._try_edit_pending_result(pending.result_id)

    async def handle_status_callback(self, callback_query: CallbackQuery) -> None:
        task_id = parse_inline_status_task_id(callback_query.data)
        inline_message_id = callback_query.inline_message_id
        if not task_id or not inline_message_id:
            await callback_query.answer()
            return

        logger.info("Inline status callback received for task_id=%s", task_id)
        pending = await self._get_pending_result(task_id)

        try:
            task = await self._api_client.get_task(task_id)
        except ApiClientError as exc:
            await callback_query.answer("Task is unavailable.", show_alert=False)
            logger.warning("Failed to fetch inline task %s from callback: %s", task_id, exc)
            return

        if task.status not in TERMINAL_STATUSES:
            try:
                await self._edit_inline_message(
                    inline_message_id=inline_message_id,
                    text=format_inline_executing_message(
                        status=task.status,
                    ),
                    reply_markup=inline_status_reply_markup(task_id),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to refresh inline progress from callback for %s: %s",
                    task_id,
                    exc,
                )
            await callback_query.answer(f"Still {task.status.replace('_', ' ')}.", show_alert=False)
            return

        try:
            await self._edit_inline_message(
                inline_message_id=inline_message_id,
                text=format_inline_execution_message(
                    pending.code if pending is not None else "",
                    task,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to edit inline message from callback for %s: %s", task_id, exc)
            await callback_query.answer("Failed to update result.", show_alert=False)
            return

        await callback_query.answer("Result updated.", show_alert=False)
        async with self._lock:
            self._pending_results.pop(task_id, None)

    async def _advance_revision(self, user_id: int) -> tuple[int, str | None]:
        async with self._lock:
            session = self._sessions.setdefault(user_id, InlineSession())
            session.revision += 1
            previous_task_id = session.active_task_id
            session.active_task_id = None
            return session.revision, previous_task_id

    async def _is_current(self, user_id: int, revision: int) -> bool:
        async with self._lock:
            session = self._sessions.get(user_id)
            return session is not None and session.revision == revision

    async def _set_active_task(self, user_id: int, revision: int, task_id: str) -> bool:
        async with self._lock:
            session = self._sessions.get(user_id)
            if session is None or session.revision != revision:
                return False
            session.active_task_id = task_id
            return True

    async def _clear_if_current(self, user_id: int, revision: int) -> None:
        async with self._lock:
            session = self._sessions.get(user_id)
            if session is None or session.revision != revision:
                return
            session.active_task_id = None

    async def _clear_active_task_if_current(
        self,
        user_id: int,
        revision: int,
        task_id: str,
    ) -> None:
        async with self._lock:
            session = self._sessions.get(user_id)
            if session is None or session.revision != revision:
                return
            if session.active_task_id == task_id:
                session.active_task_id = None

    async def _safe_cancel(self, task_id: str) -> None:
        try:
            await self._api_client.cancel_task(task_id)
        except ApiClientError as exc:
            logger.warning("Failed to cancel inline task %s: %s", task_id, exc)

    async def _answer_with_hint(self, query: InlineQuery, message: str) -> None:
        await self._safe_answer_results(query, [inline_hint_result(message)])

    async def _safe_answer_results(self, query: InlineQuery, results: list) -> None:
        try:
            await query.answer(
                results=results,
                is_personal=True,
                cache_time=self._cache_time_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to answer inline query %s: %s", query.id, exc)

    async def _safe_answer_empty(self, query: InlineQuery) -> None:
        await self._safe_answer_results(query, [])

    async def _resolve_pending_result(
        self,
        *,
        pending: InlinePendingResult,
        user_id: int,
        revision: int,
    ) -> None:
        try:
            task = await self._api_client.wait_for_terminal(
                pending.task_id,
                poll_interval_seconds=self._poll_interval_seconds,
            )
            pending.final_message_text = format_inline_execution_message(pending.code, task)
        except ApiClientError as exc:
            pending.final_message_text = format_inline_request_error_message(str(exc))
        finally:
            await self._clear_active_task_if_current(user_id, revision, pending.task_id)
            await self._store_pending_result(pending)
            await self._try_edit_pending_result(pending.result_id)

    async def _get_pending_result(self, result_id: str) -> InlinePendingResult | None:
        async with self._lock:
            return self._pending_results.get(result_id)

    async def _store_pending_result(self, pending: InlinePendingResult) -> None:
        async with self._lock:
            self._pending_results[pending.result_id] = pending

    async def _try_edit_pending_result(self, result_id: str) -> None:
        pending = await self._get_pending_result(result_id)
        if pending is None:
            return
        if pending.edit_applied or not pending.inline_message_id or not pending.final_message_text:
            return

        try:
            await self._edit_inline_message(
                inline_message_id=pending.inline_message_id,
                text=pending.final_message_text,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to edit inline message for %s: %s", result_id, exc)
            return

        pending.edit_applied = True
        async with self._lock:
            self._pending_results.pop(result_id, None)

    async def _edit_inline_message(
        self,
        *,
        inline_message_id: str,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        await self._bot.edit_message_text(
            inline_message_id=inline_message_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

    async def _refresh_inline_progress(self, task_id: str, inline_message_id: str) -> None:
        try:
            task = await self._api_client.get_task(task_id)
        except ApiClientError as exc:
            logger.warning("Failed to refresh inline progress for %s: %s", task_id, exc)
            return

        if task.status in TERMINAL_STATUSES:
            return

        try:
            await self._edit_inline_message(
                inline_message_id=inline_message_id,
                text=format_inline_executing_message(
                    status=task.status,
                ),
                reply_markup=inline_status_reply_markup(task_id),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to edit inline progress for %s: %s", task_id, exc)

    def _track_background_task(self, coroutine: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
