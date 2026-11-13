"""Secret hiding. Every tool result passes through here before the model or the trace sees it."""

from __future__ import annotations

import re

REDACTED = "<redacted>"
TOKEN = r"sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
SECRET_NAME = re.compile(r"pass|secret|token|key|credential|auth|private|dsn|cert", re.IGNORECASE)
SECRET_VALUE = re.compile(rf"^(?:{TOKEN})$|^[A-Za-z0-9+/=_-]{{32,}}$")
SECRET_IN_TEXT = (
    (re.compile(r"(://[^/\s:@]+:)[^/\s@]+@"), r"\1" + REDACTED + "@"),
    (re.compile(r"((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S+", re.IGNORECASE), r"\1" + REDACTED),
    (re.compile(rf"\b(?:{TOKEN})"), REDACTED),
)


def visible_value(name: str, value: str) -> str:
    """A setting as the model and the trace may see it."""
    if SECRET_NAME.search(name) or SECRET_VALUE.search(value) or redact_text(value) != value:
        return REDACTED
    return value


def redact_text(text: str) -> str:
    for pattern, replacement in SECRET_IN_TEXT:
        text = pattern.sub(replacement, text)
    return text


def redact_payload(value):
    """Every string inside a tool result: logs, event messages, and error text alike."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_payload(item) for key, item in value.items()}
    return value
