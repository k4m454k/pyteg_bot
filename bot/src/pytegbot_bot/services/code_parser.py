from __future__ import annotations

import re

CODE_BLOCK_RE = re.compile(r"```(?:python3?|py3?)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
COMMAND_RE = re.compile(r"^/code(?:@(?P<username>[A-Za-z0-9_]+))?\b", re.IGNORECASE)


def normalize_code(candidate: str) -> str | None:
    text = candidate.strip()
    if not text:
        return None

    match = CODE_BLOCK_RE.search(text)
    if match:
        text = match.group(1).strip()

    return text or None


def extract_code_from_command(text: str, bot_username: str) -> str | None:
    payload = text.lstrip()
    match = COMMAND_RE.match(payload)
    if not match:
        return None

    target_username = match.group("username")
    if target_username and target_username.lower() != bot_username.lstrip("@").lower():
        return None

    code_payload = payload[match.end() :].strip(" \n:-")
    return normalize_code(code_payload)


def extract_code_from_message(text: str, bot_username: str) -> str | None:
    username = bot_username.lstrip("@")
    mention_pattern = re.compile(rf"@{re.escape(username)}\b", re.IGNORECASE)
    if not mention_pattern.search(text):
        return None

    payload = mention_pattern.sub("", text, count=1).strip(" \n:-")
    return normalize_code(payload)


def extract_code_from_inline_query(text: str) -> str | None:
    return normalize_code(text)
