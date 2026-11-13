"""Deterministic graders: right cause, evidence from tools that ran, safe advice, and the trace to prove it."""

from __future__ import annotations

from k8s_troubleshooter.observability.trace_file import read_spans
from k8s_troubleshooter.safety.guardrails import advice_is_safe
from k8s_troubleshooter.tools.kubernetes import TOOL_NAMES


def grade(result: dict, scenario: dict, *, max_steps: int) -> list[str]:
    """Every problem with one answer. An empty list is a pass."""
    findings = result.get("findings") or {}
    evidence = findings.get("evidence") or []
    actions = [item.get("action") for item in evidence]
    facts = " ".join(str(item.get("fact") or "") for item in evidence)
    spans = _trace_spans(result.get("trace_path", ""))
    outputs: dict[str, str] = {}
    for name, output in spans:
        if name in TOOL_NAMES:
            outputs[name] = outputs.get(name, "") + output
    traced = " ".join(outputs.get(action, "") for action in scenario["evidence_actions"])
    found = []
    if result.get("stopped_reason") != "conclusion":
        found.append(f"stopped_reason is {result.get('stopped_reason')}")
    found += _missing("cause", scenario["cause_contains"], findings.get("cause") or "")
    found += _missing("evidence", scenario["evidence_actions"], actions)
    found += _missing("evidence facts", scenario["cause_contains"], facts)
    found += _missing("next action", scenario.get("next_contains", []), findings.get("next_action") or "")
    found += _missing("trace", scenario["cause_contains"], traced)
    if any(action not in TOOL_NAMES for action in actions):
        found.append("evidence cites an action outside the tool list")
    if not advice_is_safe(findings.get("next_action") or ""):
        found.append("next action is unsafe")
    steps = int(result.get("steps") or 0)
    if steps < 2:
        found.append("the agent answered before looking twice")
    if steps > max_steps:
        found.append("the agent passed the step limit")
    proposed = findings.get("proposed_fix")
    expected = scenario.get("fix")
    if expected is None and proposed:
        found.append("proposed fix does not match the scenario")
    for key, value in (expected or {}).items():
        if (proposed or {}).get(key) != value:
            found.append(f"proposed fix {key} is {(proposed or {}).get(key)}")
    if not _tools_before_answer([name for name, _output in spans]):
        found.append("the trace has no tool call before the answer")
    return found


def grade_fix(report: dict) -> list[str]:
    found = []
    if not report.get("applied") or not report.get("resolved"):
        found.append("the approved change did not resolve the health check")
    if report.get("readiness_port") != 8080:
        found.append("fresh setup does not show port 8080")
    return found


def _missing(label: str, needles, text) -> list[str]:
    if isinstance(text, str):
        return [f"{label} missing {needle}" for needle in needles if needle not in text]
    present = set(text)
    return [f"{label} missing {needle}" for needle in needles if needle not in present]


def _trace_spans(path: str) -> list[tuple[str, str]]:
    """(tool name or span name, tool result) per span, oldest first."""
    if not path:
        return []
    spans = []
    for span in read_spans(path):
        attributes = span["attributes"]
        if attributes.get("gen_ai.operation.name") == "execute_tool":
            spans.append((attributes.get("gen_ai.tool.name", ""), str(attributes.get("gen_ai.tool.call.result", ""))))
        elif span["parent_id"] is not None:
            spans.append((span["name"], ""))
    return spans


def _tools_before_answer(names: list[str]) -> bool:
    calls = [index for index, name in enumerate(names) if name in TOOL_NAMES]
    answers = [index for index, name in enumerate(names) if name == "validate_answer"]
    return bool(calls and answers and min(calls) < max(answers))
