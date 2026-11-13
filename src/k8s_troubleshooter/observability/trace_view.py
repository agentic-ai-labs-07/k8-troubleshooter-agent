"""Turn one trace file into a report a person can read: detailed JSON, or a short table."""

from __future__ import annotations

import json
import re
from pathlib import Path

from k8s_troubleshooter.config import trace_dir
from k8s_troubleshooter.observability.log_file import CONTEXT_KEYS, read_entries
from k8s_troubleshooter.observability.trace_file import read_spans, root_span

RUN_ID = re.compile(r"(approve-)?[0-9a-f]{32}")
APP = "k8s_troubleshooter."
TABLE_CHARS = 110


def trace_path(run_id: str) -> Path:
    """`latest`, a run id, or `approve-<proposal id>`. Never a free path."""
    folder = trace_dir()
    if run_id == "latest":
        runs = _runs(folder)
        if not runs:
            raise FileNotFoundError(f"no traces yet in {folder}")
        return runs[0]
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("give latest, a 32-character run id, or approve-<proposal id>")
    path = folder / f"{run_id}.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"no trace for {run_id} in {folder}")
    return path


def list_runs(limit: int = 10) -> list[dict]:
    """The newest runs, newest first."""
    runs = []
    for path in _runs(trace_dir())[:limit]:
        try:
            root = root_span(read_spans(path)) or {"attributes": {}, "start": 0, "end": 0, "status": "ok"}
        except (ValueError, KeyError, json.JSONDecodeError):
            runs.append({"run_id": path.stem, "result": "old trace format"})
            continue
        attributes = root["attributes"]
        runs.append({
            "run_id": path.stem,
            "operation": root.get("name", ""),
            "question": attributes.get(APP + "question") or attributes.get(APP + "change", ""),
            "result": attributes.get(APP + "stopped_reason", "unknown"),
            "status": root["status"],
            "duration_ms": _ms(root["end"] - root["start"]),
        })
    return runs


def report(path: Path) -> dict:
    """request → agent → model → tool calls → response, as one JSON object."""
    spans = read_spans(path)
    root = root_span(spans)
    if root is None:
        raise ValueError(f"{path.name} has no root span; the run may still be going")
    steps = [span for span in spans if span is not root]
    attributes = root["attributes"]
    tokens_in = sum(int(span["attributes"].get("gen_ai.usage.input_tokens", 0)) for span in steps)
    tokens_out = sum(int(span["attributes"].get("gen_ai.usage.output_tokens", 0)) for span in steps)
    tools = [span["attributes"].get("gen_ai.tool.name") for span in steps if _operation(span) == "execute_tool"]
    body = {
        "run_id": path.stem,
        "trace_id": root["trace_id"],
        "trace_file": str(path),
        "request": _drop_empty({
            "operation": root["name"],
            "question": attributes.get(APP + "question"),
            "change": attributes.get(APP + "change"),
            "agent": attributes.get("gen_ai.agent.name"),
            "model": attributes.get("gen_ai.request.model"),
            "provider": attributes.get("gen_ai.provider.name"),
            "step_limit": attributes.get(APP + "step_limit"),
        }),
        "summary": {
            "result": attributes.get(APP + "stopped_reason", "unknown"),
            "status": root["status"],
            "duration_ms": _ms(root["end"] - root["start"]),
            "model_calls": sum(1 for span in steps if _operation(span) == "chat"),
            "tool_calls": len(tools),
            "tools_used": list(dict.fromkeys(tools)),
            "errors": sum(1 for span in spans if span["status"] == "error"),
            "tokens": {"input": tokens_in, "output": tokens_out, "total": tokens_in + tokens_out},
        },
        "steps": [_step(number, span, root["start"]) for number, span in enumerate(steps, 1)],
        "response": _load(attributes.get(APP + "findings")) or _drop_empty({
            "resolved": attributes.get(APP + "resolved"),
            "readiness_port": attributes.get(APP + "readiness_port"),
            "detail": attributes.get(APP + "detail"),
        }),
    }
    if root["links"]:
        body["linked_trace"] = root["links"][0]
    if root["status"] == "error":
        body["summary"]["error"] = root["status_message"]
    numbers = {span["span_id"]: number for number, span in enumerate(steps, 1)}
    body["logs"] = [_log_line(entry, numbers) for entry in read_entries() if entry.get("run_id") == path.stem]
    return body


def _log_line(entry: dict, numbers: dict) -> dict:
    """One log line of this run, pointing at its step. Lines outside any step belong to the run itself."""
    return {
        "time": entry.get("timestamp"),
        "level": entry.get("level"),
        "event": entry.get("event"),
        "step": numbers.get(entry.get("span_id")),
        "service": entry.get("service"),
        **{key: value for key, value in entry.items() if key not in CONTEXT_KEYS},
    }


def table(body: dict) -> str:
    """The same report, one line per step."""
    request, summary = body["request"], body["summary"]
    tokens = summary["tokens"]
    lines = [
        f"Run       {body['run_id']}",
        f"Question  {request.get('question') or request.get('change', '')}",
    ]
    if request.get("model"):
        lines.append(f"Model     {request['model']} ({request.get('provider', '')})")
    lines.append(f"Result    {summary['result']}" + (f" (error: {summary['error']})" if summary.get("error") else ""))
    took = f"Took      {summary['duration_ms'] / 1000:.1f} s, {_count(summary['model_calls'], 'model call')}, {_count(summary['tool_calls'], 'tool call')}"
    if tokens["total"]:
        took += f", {tokens['total']} tokens ({tokens['input']} in, {tokens['output']} out)"
    lines += [took, "", "  #  At      Step                              Took     What happened"]
    for step in body["steps"]:
        lines.append(
            f"{step['step']:>3}  +{step['started_at_ms'] / 1000:<5.1f}  {step['span']:<32}  {step['duration_ms']:>5.0f} ms  {_clip(step_summary(step))}"
        )
    lines += ["", *_answer(body["response"])]
    if body.get("logs"):
        warnings = sum(1 for entry in body["logs"] if entry["level"] in ("WARNING", "ERROR", "CRITICAL"))
        lines += ["", f"Logs      {_count(len(body['logs']), 'line')}, {_count(warnings, 'warning')}: logs --run {body['run_id']}"]
    return "\n".join(lines)


def _step(number: int, span: dict, start: int) -> dict:
    attributes = span["attributes"]
    operation = _operation(span)
    step = {
        "step": number,
        "type": {"chat": "model", "execute_tool": "tool"}.get(operation, "check"),
        "span": span["name"],
        "started_at_ms": _ms(span["start"] - start),
        "duration_ms": _ms(span["end"] - span["start"]),
        "status": span["status"],
    }
    if operation == "chat":
        if "gen_ai.usage.input_tokens" in attributes:
            step["tokens"] = {"input": attributes["gen_ai.usage.input_tokens"], "output": attributes.get("gen_ai.usage.output_tokens")}
        step["finish_reason"] = (attributes.get("gen_ai.response.finish_reasons") or [None])[0]
        parts = [part for message in _load(attributes.get("gen_ai.output.messages")) or [] for part in message.get("parts", [])]
        calls = [part for part in parts if part.get("type") == "tool_call"]
        if calls:
            step["decision"] = {"call_tool": calls[0].get("name"), "arguments": calls[0].get("arguments"), "call_id": calls[0].get("id")}
        elif parts:
            step["decision"] = {"answer": _load(_json_text(parts[0].get("content", "")))}
    elif operation == "execute_tool":
        step["tool"] = attributes.get("gen_ai.tool.name")
        step["arguments"] = _load(attributes.get("gen_ai.tool.call.arguments"))
        step["result"] = _load(attributes.get("gen_ai.tool.call.result"))
    else:
        step.update({key[len(APP):]: _load(value) for key, value in attributes.items() if key.startswith(APP)})
    if span["status"] == "error":
        step["error"] = _drop_empty({"type": attributes.get("error.type"), "message": span["status_message"]})
    return step


def step_summary(step: dict) -> str:
    """What one step did, in a few words."""
    if step.get("error"):
        return f"FAILED {step['error'].get('type', '')}: {step['error'].get('message', '')}"
    decision = step.get("decision") or {}
    if "call_tool" in decision:
        return f"decides to call {decision['call_tool']} {_args(decision.get('arguments') or {})}".rstrip()
    if "answer" in decision:
        return "writes the answer"
    if step["type"] == "tool":
        return _tool_summary(step["tool"], step.get("result"))
    if step["span"] == "validate_answer":
        checks = step.get("checks") or []
        failed = [item["check"] for item in checks if not item.get("passed")]
        return f"answer {step.get('outcome', 'checked')}, {len(checks) - len(failed)} of {len(checks)} checks passed" + (f"; failed: {', '.join(failed)}" if failed else "")
    if step["span"] == "recheck":
        return f"fresh read: resolved={step.get('resolved')}, health check on {step.get('readiness_port')}"
    return ""


def _tool_summary(tool: str, result) -> str:
    if not isinstance(result, dict):
        return str(result or "")
    if result.get("error"):
        return f"FAILED: {result['error']}"
    if tool == "get_pod_status":
        pods = result.get("pods", [])
        broken = [f"{pod['namespace']}/{pod['name']}" for pod in pods if not pod.get("ready") and not pod.get("deleting")]
        return f"{len(pods)} pods, {len(broken)} not ready" + (": " + ", ".join(broken[:3]) if broken else "")
    if tool == "get_events":
        events = result.get("events", [])
        problems = [item for item in events if item.get("reason") not in ("Scheduled", "Pulling", "Pulled", "Created", "Started")]
        last = (problems or events)[-1:] or [{"reason": "none", "message": "no events"}]
        return f"{_count(len(events), 'event')}, last problem {last[0]['reason']}: {last[0]['message']}"
    if tool == "get_pod_logs":
        lines = [line for line in str(result.get("logs", "")).splitlines() if line.strip()]
        return f"last log line: {lines[-1]}" if lines else "no log lines"
    if tool == "get_setup":
        if not result.get("found"):
            return "workload not found"
        parts = []
        for item in result.get("containers", []):
            missing = [config["name"] for config in item.get("configmaps", []) if not config.get("found")]
            parts.append(f"{item.get('name')}: listens on {item.get('ports')}, health check on {item.get('readiness_port')}" + (f", missing settings {missing}" if missing else ""))
        return "; ".join(parts)
    if tool == "patch_readiness_port":
        return f"patched {result.get('namespace')}/{result.get('deployment')}: health check now on {result.get('port')}"
    return json.dumps(result)


def _answer(response) -> list[str]:
    if not isinstance(response, dict) or not response:
        return ["Answer    none"]
    if "resolved" in response:
        return ["Answer", f"  Resolved:        {'yes' if response['resolved'] else 'no'}", f"  Health check on: {response.get('readiness_port')}"]
    if "cause" not in response:
        return ["Answer", f"  {response.get('detail', json.dumps(response))}"]
    lines = ["Answer", f"  Cause:        {response.get('cause', '')}"]
    lines += [f"  Evidence:     [{item.get('action')}] {_clip(item.get('fact', ''), 200)}" for item in response.get("evidence", [])]
    lines.append(f"  Next step:    {response.get('next_action', '')}")
    fix = response.get("proposed_fix")
    lines.append(
        f"  Proposed fix: port {fix.get('from_port')} to {fix.get('to_port')} on {fix.get('namespace')}/{fix.get('deployment')}. Run approve to apply it."
        if fix else "  Proposed fix: none"
    )
    return lines


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _runs(folder: Path) -> list[Path]:
    return sorted(folder.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)


def _operation(span: dict) -> str:
    return span["attributes"].get("gen_ai.operation.name", "")


def _ms(nanoseconds: int) -> float:
    return round(nanoseconds / 1e6, 1)


def _json_text(text: str) -> str:
    """The model's answer can wrap its JSON in prose. Keep the JSON part when there is one."""
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text


def _load(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def _drop_empty(values: dict) -> dict:
    return {key: value for key, value in values.items() if value not in (None, "")}


def _args(args: dict) -> str:
    return "(" + ", ".join(str(value) for value in args.values()) + ")" if args else ""


def _clip(value, limit: int = TABLE_CHARS) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
