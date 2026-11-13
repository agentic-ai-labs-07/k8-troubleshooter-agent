"""Read a trace file back: OTLP/JSON lines in, plain span dicts out, oldest first."""

from __future__ import annotations

import json
from pathlib import Path

KINDS = {0: "unspecified", 1: "internal", 2: "server", 3: "client", 4: "producer", 5: "consumer"}
STATUSES = {0: "ok", 1: "ok", 2: "error"}


def read_spans(path: str | Path) -> list[dict]:
    path = Path(path)
    spans = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        request = json.loads(line)
        if "resourceSpans" not in request:
            raise ValueError(f"{path.name} uses the old trace format. Run the agent again for a new trace.")
        for resource in request["resourceSpans"]:
            for scope in resource.get("scopeSpans", []):
                spans += [_span(item) for item in scope.get("spans", [])]
    return sorted(spans, key=lambda span: span["start"])


def root_span(spans: list[dict]) -> dict | None:
    return next((span for span in spans if span["parent_id"] is None), None)


def _span(item: dict) -> dict:
    status = item.get("status") or {}
    return {
        "name": item["name"],
        "trace_id": item["traceId"],
        "span_id": item["spanId"],
        "parent_id": item.get("parentSpanId") or None,
        "kind": KINDS.get(int(item.get("kind", 0)), "unspecified"),
        "start": int(item["startTimeUnixNano"]),
        "end": int(item["endTimeUnixNano"]),
        "status": STATUSES.get(int(status.get("code", 0)), "ok"),
        "status_message": status.get("message", ""),
        "attributes": _attributes(item.get("attributes", [])),
        "events": [{"name": entry["name"], "attributes": _attributes(entry.get("attributes", []))} for entry in item.get("events", [])],
        "links": [{"trace_id": entry["traceId"], "span_id": entry["spanId"]} for entry in item.get("links", [])],
    }


def _attributes(items: list[dict]) -> dict:
    return {item["key"]: _value(item.get("value") or {}) for item in items}


def _value(value: dict):
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "arrayValue" in value:
        return [_value(item) for item in value["arrayValue"].get("values", [])]
    return None
