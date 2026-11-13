"""OpenTelemetry tracing: one OTLP JSON file per run, with GenAI span names and attributes.

The tree for one question:

    invoke_agent k8s-troubleshooter      the request and the final answer
      chat gpt-4.1-mini                  one model call: tokens and what it decided
      execute_tool get_pod_status        one tool call: arguments and result
      ...
      validate_answer                    the checks the answer passed or failed

Each line of traces/<run_id>.jsonl is an OTLP ExportTraceServiceRequest, the format an
OpenTelemetry Collector, Jaeger, or Grafana Tempo can import.
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from opentelemetry import context as otel_context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import Link, SpanContext, SpanKind, StatusCode, TraceFlags, set_span_in_context
from opentelemetry.trace.status import Status

from k8s_troubleshooter import __version__
from k8s_troubleshooter.config import trace_dir
from k8s_troubleshooter.observability.logs import bind_run, event, unbind_run

SERVICE_NAME = "k8s-troubleshooter"
APP = "k8s_troubleshooter."
MAX_ATTRIBUTE_CHARS = 16000

_current_tracer = contextvars.ContextVar("current_tracer", default=None)
_current_run = contextvars.ContextVar("current_run", default=None)


class RunTrace:
    """One run: its file, its root span, and the totals for the closing log line."""

    def __init__(self, run_id: str, path: Path, root):
        self.run_id = run_id
        self.path = path
        self.root = root
        context = root.get_span_context()
        self.trace_id = format(context.trace_id, "032x")
        self.span_id = format(context.span_id, "016x")
        self.tools_used: list[str] = []
        self.tool_errors = 0
        self.model_calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.summary: dict = {}

    def finish(self, **fields) -> None:
        """Results of the run, on the root span and in the run_finished log line."""
        for key, value in fields.items():
            if value is not None:
                self.root.set_attribute(APP + key, _value(value))
        self.summary.update({key: value for key, value in fields.items() if value is not None and key not in ("findings", "fresh_evidence")})


class Step:
    """One span. Outside a run every method does nothing, so tools also work in plain tests."""

    def __init__(self, span=None, fields: dict | None = None):
        self.span = span
        self.fields = fields if fields is not None else {}
        self.output = ""
        self.failed = False

    def set(self, key: str, value) -> None:
        if self.span is not None and value is not None:
            self.span.set_attribute(key, _value(value))

    def fail(self, error_type: str, message: str) -> None:
        """A failure the code handled, such as a refused read. It still marks the span as an error."""
        self.failed = True
        self.fields["error"] = message[:300]
        if self.span is not None:
            self.span.set_attribute("error.type", error_type)
            self.span.set_status(Status(StatusCode.ERROR, message[:500]))

    def model_response(self, response) -> None:
        """Usage, finish reason, and the decision: a tool call or the answer text."""
        calls = list(getattr(response, "tool_calls", None) or [])
        metadata = getattr(response, "response_metadata", None) or {}
        usage = getattr(response, "usage_metadata", None) or {}
        if calls:
            parts = [{"type": "tool_call", "id": call.get("id"), "name": call.get("name"), "arguments": call.get("args") or {}} for call in calls]
            finish = "tool_call"
            self.fields["decision"] = calls[0].get("name")
        else:
            parts = [{"type": "text", "content": str(response.content)}]
            finish = "stop"
            self.fields["decision"] = "answer"
        finish = str(metadata.get("finish_reason") or finish)
        self.set("gen_ai.output.messages", [{"role": "assistant", "parts": parts, "finish_reason": finish}])
        self.set("gen_ai.response.finish_reasons", [finish])
        self.set("gen_ai.response.model", metadata.get("model_name"))
        self.set("gen_ai.response.id", metadata.get("id") or getattr(response, "id", None))
        run = _current_run.get()
        for key in ("input_tokens", "output_tokens"):
            if key in usage:
                self.set(f"gen_ai.usage.{key}", int(usage[key]))
                self.fields[key] = int(usage[key])
                if run is not None:
                    setattr(run, key, getattr(run, key) + int(usage[key]))
        self.output = json.dumps(parts, default=str)

    def tool_result(self, text: str) -> None:
        self.set("gen_ai.tool.call.result", text)
        self.output = text


@contextmanager
def tracing(run_id: str, name: str, attributes: dict, *, link: dict | None = None):
    """The root span of one run, written to traces/<run_id>.jsonl."""
    path = trace_dir() / f"{run_id}.jsonl"
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME, "service.version": __version__}))
    provider.add_span_processor(SimpleSpanProcessor(_OtlpFileExporter(path)))
    tracer = provider.get_tracer(SERVICE_NAME, __version__)
    root = tracer.start_span(name, kind=SpanKind.INTERNAL, attributes=_attributes(attributes), links=_links(link))
    run = RunTrace(run_id, path, root)
    tokens = (
        otel_context.attach(set_span_in_context(root)),
        _current_tracer.set(tracer),
        _current_run.set(run),
        bind_run(run_id),
    )
    started = time.perf_counter()
    try:
        yield run
    except Exception as exc:
        _record_exception(root, exc)
        run.summary["error"] = str(exc)[:300]
        raise
    finally:
        event(
            "run_finished",
            logging.WARNING if run.summary.get("error") else logging.INFO,
            **run.summary,
            model_calls=run.model_calls,
            tool_calls=len(run.tools_used),
            tools_used=sorted(set(run.tools_used)),
            tool_errors=run.tool_errors,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            latency_ms=round((time.perf_counter() - started) * 1000),
            trace_id=run.trace_id,
            trace_path=str(path),
        )
        if run.model_calls:
            root.set_attribute("gen_ai.usage.input_tokens", run.input_tokens)
            root.set_attribute("gen_ai.usage.output_tokens", run.output_tokens)
        root.end()
        provider.shutdown()
        unbind_run(tokens[3])
        _current_run.reset(tokens[2])
        _current_tracer.reset(tokens[1])
        otel_context.detach(tokens[0])


@contextmanager
def model_step(model: str, provider: str, *, max_tokens: int | None, input_messages: int):
    attributes = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        "gen_ai.request.max_tokens": max_tokens,
        APP + "input_messages": input_messages,
    }
    try:
        with _span(f"chat {model}", SpanKind.CLIENT, attributes, "model_call", {"model": model}) as step:
            yield step
    finally:
        run = _current_run.get()
        if run is not None:
            run.model_calls += 1


@contextmanager
def tool_step(name: str, arguments: dict, **extra):
    attributes = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": name,
        "gen_ai.tool.type": "function",
        "gen_ai.tool.call.arguments": arguments,
        **{APP + key: value for key, value in extra.items()},
    }
    step = Step()
    try:
        with _span(f"execute_tool {name}", SpanKind.INTERNAL, attributes, "tool_call", {"tool": name, "arguments": arguments}) as step:
            yield step
    finally:
        run = _current_run.get()
        if run is not None:
            run.tools_used.append(name)
            run.tool_errors += int(step.failed)


@contextmanager
def traced_step(name: str, **attributes):
    """A step that is not a model or tool call, such as validate_answer or recheck."""
    with _span(name, SpanKind.INTERNAL, {APP + key: value for key, value in attributes.items()}, name, {}) as step:
        yield step


@contextmanager
def _span(name: str, kind: SpanKind, attributes: dict, event_name: str, fields: dict):
    tracer = _current_tracer.get()
    if tracer is None:
        yield Step()
        return
    started = time.perf_counter()
    with tracer.start_as_current_span(name, kind=kind, attributes=_attributes(attributes), record_exception=False, set_status_on_exception=False) as span:
        step = Step(span, dict(fields))
        try:
            yield step
        except Exception as exc:
            _record_exception(span, exc)
            step.failed = True
            step.fields["error"] = str(exc)[:300]
            raise
        finally:
            latency = round((time.perf_counter() - started) * 1000, 1)
            status = "error" if step.failed else "ok"
            event(event_name, logging.WARNING if step.failed else logging.INFO, **step.fields, status=status, latency_ms=latency)
            if step.output:
                event(event_name + "_output", logging.DEBUG, output=step.output[:2000])


def _record_exception(span, exc: BaseException) -> None:
    span.record_exception(exc)
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR, str(exc)[:500]))


def _links(link: dict | None) -> list[Link]:
    """Ties an approve run back to the investigation that proposed the change."""
    if not link or not link.get("trace_id") or not link.get("span_id"):
        return []
    context = SpanContext(int(link["trace_id"], 16), int(link["span_id"], 16), is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED))
    return [Link(context, {APP + "link.reason": "proposed_by"})]


def _attributes(attributes: dict) -> dict:
    return {key: _value(value) for key, value in attributes.items() if value is not None}


def _value(value):
    """OpenTelemetry attributes are primitives or lists of strings. Anything structured becomes JSON text."""
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clip(value)
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return [_clip(item) for item in value]
    return _clip(json.dumps(value, default=str))


def _clip(text: str) -> str:
    if len(text) <= MAX_ATTRIBUTE_CHARS:
        return text
    return text[:MAX_ATTRIBUTE_CHARS] + f"…[{len(text) - MAX_ATTRIBUTE_CHARS} more characters]"


class _OtlpFileExporter(SpanExporter):
    """Append each finished span as one OTLP/JSON line."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def export(self, spans) -> SpanExportResult:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            for span in spans:
                handle.write(json.dumps(_otlp_request(span)) + "\n")
        return SpanExportResult.SUCCESS


def _otlp_request(span) -> dict:
    scope = span.instrumentation_scope
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _otlp_attributes(span.resource.attributes)},
                "scopeSpans": [
                    {
                        "scope": {"name": scope.name if scope else SERVICE_NAME, "version": (scope.version if scope else None) or __version__},
                        "spans": [_otlp_span(span)],
                    }
                ],
            }
        ]
    }


def _otlp_span(span) -> dict:
    context = span.context
    item = {
        "traceId": format(context.trace_id, "032x"),
        "spanId": format(context.span_id, "016x"),
        "name": span.name,
        "kind": span.kind.value + 1,
        "startTimeUnixNano": str(span.start_time),
        "endTimeUnixNano": str(span.end_time),
        "attributes": _otlp_attributes(span.attributes),
        "status": {"code": span.status.status_code.value},
    }
    if span.parent is not None:
        item["parentSpanId"] = format(span.parent.span_id, "016x")
    if span.status.description:
        item["status"]["message"] = span.status.description
    if span.events:
        item["events"] = [
            {"timeUnixNano": str(entry.timestamp), "name": entry.name, "attributes": _otlp_attributes(entry.attributes)}
            for entry in span.events
        ]
    if span.links:
        item["links"] = [
            {
                "traceId": format(entry.context.trace_id, "032x"),
                "spanId": format(entry.context.span_id, "016x"),
                "attributes": _otlp_attributes(entry.attributes),
            }
            for entry in span.links
        ]
    return item


def _otlp_attributes(attributes) -> list[dict]:
    return [{"key": key, "value": _otlp_value(value)} for key, value in dict(attributes or {}).items()]


def _otlp_value(value) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_otlp_value(item) for item in value]}}
    return {"stringValue": str(value)}
