"""Structured logs: one event per line, tied to the run and the trace, with secrets hidden.

Lines go to stderr (text or JSON, see LOG_FORMAT) and, always as JSON, to one shared log file,
so the `logs` command can show every run from both containers in one place.
"""

from __future__ import annotations

import contextvars
import json
import logging
import logging.handlers
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

from opentelemetry import trace

from k8s_troubleshooter.config import log_file, log_format, log_level
from k8s_troubleshooter.safety.redaction import redact_payload

LOGGER_NAME = "k8s_troubleshooter"
MAX_FILE_BYTES = 5 * 1024 * 1024
KEPT_FILES = 3
_run_id = contextvars.ContextVar("run_id", default="")
_HANDLER_MARK = "_k8s_troubleshooter_handler"
_process: dict = {}

log = logging.getLogger(LOGGER_NAME)


def setup_logging(command: str = "") -> None:
    """Our events at LOG_LEVEL. Other libraries stay at WARNING: their debug output can include request details."""
    root = logging.getLogger()
    for handler in [item for item in root.handlers if getattr(item, _HANDLER_MARK, False)]:
        root.removeHandler(handler)
        handler.close()
    _process.update(service=os.environ.get("K8S_TROUBLESHOOTER_SERVICE", "local"), command=command)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_JsonFormatter() if log_format() == "json" else _TextFormatter())
    handlers = [console]
    try:
        path = log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        saved = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_FILE_BYTES, backupCount=KEPT_FILES, encoding="utf-8")
        saved.setFormatter(_JsonFormatter())
        handlers.append(saved)
    except OSError as exc:
        unsaved = str(exc)
    else:
        unsaved = ""
    for handler in handlers:
        setattr(handler, _HANDLER_MARK, True)
        handler.addFilter(_Context())
        root.addHandler(handler)
    root.setLevel(logging.WARNING)
    level = logging.getLevelName(log_level())
    log.setLevel(level if isinstance(level, int) else logging.INFO)
    if unsaved:
        event("log_file_unavailable", logging.WARNING, error=unsaved)


def event(name: str, level: int = logging.INFO, **fields) -> None:
    """One named event with its fields, such as event("tool_call", tool="get_events", status="ok")."""
    log.log(level, name, extra={"event_fields": fields})


def bind_run(run_id: str):
    return _run_id.set(run_id)


def unbind_run(token) -> None:
    _run_id.reset(token)


@contextmanager
def run_logs(run_id: str):
    """Tie log lines written outside the trace, such as fix_proposed, to their run."""
    token = bind_run(run_id)
    try:
        yield
    finally:
        unbind_run(token)


def text_value(value) -> str:
    if isinstance(value, str):
        return json.dumps(value) if (" " in value or not value) else value
    return json.dumps(value, default=str)


class _Context(logging.Filter):
    """Add the run id and the active span, so a log line leads to its trace step."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get()
        context = trace.get_current_span().get_span_context()
        record.trace_id = format(context.trace_id, "032x") if context.is_valid else ""
        record.span_id = format(context.span_id, "016x") if context.is_valid else ""
        return True


def _fields(record: logging.LogRecord) -> dict:
    return redact_payload(dict(getattr(record, "event_fields", {}) or {}))


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        body = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            **{key: value for key, value in _process.items() if value},
        }
        for key in ("run_id", "trace_id", "span_id"):
            if getattr(record, key, ""):
                body[key] = getattr(record, key)
        body.update(_fields(record))
        if record.exc_info:
            body["exception"] = self.formatException(record.exc_info)
        return json.dumps(body, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        time = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        parts = [f"{time} {record.levelname:<7} {record.getMessage():<16}"]
        if getattr(record, "run_id", ""):
            parts.append(f"run_id={record.run_id}")
        for key, value in _fields(record).items():
            parts.append(f"{key}={text_value(value)}")
        line = " ".join(parts)
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line
