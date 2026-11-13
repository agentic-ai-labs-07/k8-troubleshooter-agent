"""Read the shared log file back: every run, from both containers, oldest first."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from k8s_troubleshooter.config import log_file
from k8s_troubleshooter.observability.logs import KEPT_FILES, text_value

CONTEXT_KEYS = ("timestamp", "level", "logger", "event", "service", "command", "run_id", "trace_id", "span_id")
SINCE = re.compile(r"(\d+)([smhd])")
UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def read_entries(path: Path | None = None) -> list[dict]:
    path = path or log_file()
    files = [path.with_name(f"{path.name}.{number}") for number in range(KEPT_FILES, 0, -1)] + [path]
    entries = []
    for item in files:
        if item.is_file():
            entries += _parse(item.read_text(encoding="utf-8", errors="replace").splitlines())
    return entries


def select(entries: list[dict], *, run: str = "", level: str = "", name: str = "", service: str = "", since: str = "") -> list[dict]:
    lowest = _level(level) if level else 0
    after = _since(since) if since else ""
    return [
        entry for entry in entries
        if (not run or entry.get("run_id") == run)
        and _level(entry.get("level") or "INFO") >= lowest
        and (not name or entry.get("event") == name)
        and (not service or entry.get("service") == service)
        and (not after or entry.get("timestamp", "") >= after)
    ]


def format_entry(entry: dict) -> str:
    stamp = entry.get("timestamp", "")[:19].replace("T", " ")
    parts = [f"{stamp}Z {entry.get('level', ''):<7} {entry.get('service', ''):<6} {entry.get('event', ''):<16}"]
    if entry.get("run_id"):
        parts.append(f"run_id={entry['run_id']}")
    parts += [f"{key}={text_value(value)}" for key, value in entry.items() if key not in CONTEXT_KEYS]
    return " ".join(parts)


def follow(show, path: Path | None = None, poll: float = 1.0, **filters) -> None:
    """Print new lines as they arrive, like tail -f, until Ctrl-C."""
    path = path or log_file()
    position = path.stat().st_size if path.is_file() else 0
    while True:
        size = path.stat().st_size if path.is_file() else 0
        if size < position:
            position = 0
        if size > position:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                lines = handle.read().splitlines()
                position = handle.tell()
            for entry in select(_parse(lines), **filters):
                show(entry)
        time.sleep(poll)


def _parse(lines: list[str]) -> list[dict]:
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and "event" in entry:
            entries.append(entry)
    return entries


def _level(name: str) -> int:
    value = logging.getLevelName(name.strip().upper())
    if not isinstance(value, int):
        raise ValueError("level must be debug, info, warning, or error")
    return value


def _since(text: str) -> str:
    match = SINCE.fullmatch(text.strip())
    if not match:
        raise ValueError("since must look like 30s, 10m, 2h, or 1d")
    moment = datetime.now(timezone.utc) - timedelta(**{UNITS[match.group(2)]: int(match.group(1))})
    return moment.isoformat(timespec="milliseconds")
