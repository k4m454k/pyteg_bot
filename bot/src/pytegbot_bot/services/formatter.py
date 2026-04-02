from __future__ import annotations

import re
from datetime import datetime, timezone
from html import escape
from uuid import uuid4

from aiogram.enums import ParseMode
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)

from pytegbot_shared.models import ExecutionTaskResponse, TaskStatus

MAX_TELEGRAM_TEXT_CHARS = 4096
SAFE_VISIBLE_TEXT_CHARS = 3900
MAX_CODE_CHARS = 1400
MAX_ERROR_CHARS = 700
MIN_RESULT_CHARS = 200
ELLIPSIS = "..."
INLINE_STATUS_CALLBACK_PREFIX = "pytegbot_status:"

STATUS_LABELS = {
    TaskStatus.QUEUED: "Queued",
    TaskStatus.RUNNING: "Running",
    TaskStatus.SUCCEEDED: "Succeeded",
    TaskStatus.FAILED: "Failed",
    TaskStatus.CANCELLED: "Cancelled",
    TaskStatus.TIMED_OUT: "Timed out",
}


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(ELLIPSIS))] + ELLIPSIS


def visible_length(parts: list[str]) -> int:
    if not parts:
        return 0
    return sum(len(part) for part in parts) + (len(parts) - 1)


def fit_result_text(
    body_text: str,
    *,
    reserved_parts: list[str],
    safe_limit: int = SAFE_VISIBLE_TEXT_CHARS,
) -> str:
    available = max(MIN_RESULT_CHARS, safe_limit - visible_length(reserved_parts))
    return truncate(body_text, available)


def execution_body(task: ExecutionTaskResponse) -> str:
    if task.output:
        return task.output
    if task.error:
        return task.error
    return "Execution finished without output."


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"

    total_seconds = int(round(seconds))
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def execution_duration_text(task: ExecutionTaskResponse) -> str | None:
    if task.started_at is None:
        return None

    finished_at = task.finished_at or datetime.now(timezone.utc)
    duration_seconds = max(0.0, (finished_at - task.started_at).total_seconds())
    return format_duration(duration_seconds)


def format_executing_message(
    code: str,
    *,
    status: TaskStatus,
) -> str:
    parts = [
        f"<b>Status:</b> {escape(STATUS_LABELS[status])}",
    ]

    parts.extend(
        [
            "<b>Code</b>",
            f"<pre>{escape(truncate(code, MAX_CODE_CHARS))}</pre>",
        ]
    )
    return "\n".join(parts)


def format_execution_message(code: str, task: ExecutionTaskResponse) -> str:
    safe_code = truncate(code, MAX_CODE_CHARS)
    safe_error = None
    if task.error and task.error != task.output:
        safe_error = truncate(task.error, MAX_ERROR_CHARS)

    visible_parts = [f"Status: {STATUS_LABELS[task.status]}"]
    parts = [
        f"<b>Status:</b> {escape(STATUS_LABELS[task.status])}",
    ]

    if task.exit_code is not None:
        visible_parts.append(f"Exit code: {task.exit_code}")
        parts.append(f"<b>Exit code:</b> {task.exit_code}")
    execution_time = execution_duration_text(task)
    if execution_time:
        visible_parts.append(f"Execution time: {execution_time}")
        parts.append(f"<b>Execution time:</b> {execution_time}")

    visible_parts.extend(
        [
            "Code",
            safe_code,
            "Result",
        ]
    )
    if safe_error is not None:
        visible_parts.extend(["Error", safe_error])

    safe_body = fit_result_text(execution_body(task), reserved_parts=visible_parts)

    parts.extend(
        [
            "<b>Code</b>",
            f"<pre>{escape(safe_code)}</pre>",
            "<b>Result</b>",
            f"<pre>{escape(safe_body)}</pre>",
        ]
    )

    if safe_error is not None:
        parts.extend(
            [
                "<b>Error</b>",
                f"<pre>{escape(safe_error)}</pre>",
            ]
        )

    return "\n".join(parts)


def format_request_error_message(code: str, error_text: str) -> str:
    return "\n".join(
        [
            "<b>Status:</b> API error",
            "<b>Code</b>",
            f"<pre>{escape(truncate(code, MAX_CODE_CHARS))}</pre>",
            "<b>Error</b>",
            f"<pre>{escape(truncate(error_text, MAX_ERROR_CHARS))}</pre>",
        ]
    )


def format_executing_file_message(
    filename: str,
    *,
    status: TaskStatus,
) -> str:
    return "\n".join(
        [
            f"<b>Status:</b> {escape(STATUS_LABELS[status])}",
            "<b>File</b>",
            f"<pre>Accepted Python file: {escape(truncate(filename, 256))}</pre>",
        ]
    )


def format_execution_file_message(filename: str, task: ExecutionTaskResponse) -> str:
    safe_filename = truncate(filename, 256)
    safe_error = None
    if task.error and task.error != task.output:
        safe_error = truncate(task.error, MAX_ERROR_CHARS)

    visible_parts = [
        f"Status: {STATUS_LABELS[task.status]}",
        "File",
        safe_filename,
        "Result",
    ]
    parts = [
        f"<b>Status:</b> {escape(STATUS_LABELS[task.status])}",
    ]

    if task.exit_code is not None:
        visible_parts.append(f"Exit code: {task.exit_code}")
        parts.append(f"<b>Exit code:</b> {task.exit_code}")
    execution_time = execution_duration_text(task)
    if execution_time:
        visible_parts.append(f"Execution time: {execution_time}")
        parts.append(f"<b>Execution time:</b> {execution_time}")
    if safe_error is not None:
        visible_parts.extend(["Error", safe_error])

    safe_body = fit_result_text(execution_body(task), reserved_parts=visible_parts)

    parts.extend(
        [
            "<b>File</b>",
            f"<pre>{escape(safe_filename)}</pre>",
            "<b>Result</b>",
            f"<pre>{escape(safe_body)}</pre>",
        ]
    )

    if safe_error is not None:
        parts.extend(
            [
                "<b>Error</b>",
                f"<pre>{escape(safe_error)}</pre>",
            ]
        )

    return "\n".join(parts)


def format_request_error_file_message(filename: str, error_text: str) -> str:
    return "\n".join(
        [
            "<b>Status:</b> API error",
            "<b>File</b>",
            f"<pre>{escape(truncate(filename, 256))}</pre>",
            "<b>Error</b>",
            f"<pre>{escape(truncate(error_text, MAX_ERROR_CHARS))}</pre>",
        ]
    )


def format_inline_executing_message(
    *,
    status: TaskStatus,
) -> str:
    parts = [f"<b>Status:</b> {escape(STATUS_LABELS[status])}"]
    parts.append("<i>If this does not update automatically, press Refresh result.</i>")
    return "\n".join(parts)


def format_inline_execution_message(code: str, task: ExecutionTaskResponse) -> str:
    return format_execution_message(code, task)


def format_inline_request_error_message(error_text: str) -> str:
    return "\n".join(
        [
            "<b>Status:</b> API error",
            "<b>Error</b>",
            f"<pre>{escape(truncate(error_text, MAX_ERROR_CHARS))}</pre>",
        ]
    )


def inline_result_from_task(task: ExecutionTaskResponse, code: str) -> InlineQueryResultArticle:
    preview = re.sub(r"\s+", " ", execution_body(task)).strip() or "No output."
    title = truncate(f"{STATUS_LABELS[task.status]}: {preview}", 64)
    description = truncate(preview, 128)
    return InlineQueryResultArticle(
        id=uuid4().hex,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(
            message_text=format_inline_execution_message(code, task),
            parse_mode=ParseMode.HTML,
        ),
    )


def inline_executing_result(result_id: str, *, status: TaskStatus) -> InlineQueryResultArticle:
    status_label = STATUS_LABELS[status]
    return InlineQueryResultArticle(
        id=result_id,
        title=f"{status_label}...",
        description=f"Code is {status_label.lower()}. Tap refresh if needed.",
        input_message_content=InputTextMessageContent(
            message_text=format_inline_executing_message(status=status),
            parse_mode=ParseMode.HTML,
        ),
        reply_markup=inline_status_reply_markup(result_id),
    )


def inline_hint_result(message: str) -> InlineQueryResultArticle:
    safe_message = message.strip() or "Type Python code to execute."
    return InlineQueryResultArticle(
        id=uuid4().hex,
        title="PyTegBot",
        description=truncate(safe_message, 128),
        input_message_content=InputTextMessageContent(
            message_text=escape(safe_message),
            parse_mode=ParseMode.HTML,
        ),
    )


def inline_status_callback_data(task_id: str) -> str:
    return f"{INLINE_STATUS_CALLBACK_PREFIX}{task_id}"


def inline_status_reply_markup(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Refresh result",
                    callback_data=inline_status_callback_data(task_id),
                )
            ]
        ]
    )


def parse_inline_status_task_id(callback_data: str | None) -> str | None:
    if not callback_data or not callback_data.startswith(INLINE_STATUS_CALLBACK_PREFIX):
        return None
    return callback_data[len(INLINE_STATUS_CALLBACK_PREFIX) :] or None
