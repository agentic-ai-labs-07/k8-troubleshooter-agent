"""The loop: the model picks one tool, the tool reads the cluster, and the model sees the result."""

from __future__ import annotations

import json
import uuid

from langchain_core.exceptions import ModelError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from k8s_troubleshooter.agent.prompts import SYSTEM_PROMPT
from k8s_troubleshooter.agent.schemas import Findings
from k8s_troubleshooter.agent.state import AgentState
from k8s_troubleshooter.config import max_steps
from k8s_troubleshooter.exceptions import SafetyError
from k8s_troubleshooter.llm.provider import build_model, describe_model
from k8s_troubleshooter.observability.logs import event
from k8s_troubleshooter.observability.tracing import model_step, traced_step, tracing
from k8s_troubleshooter.safety.guardrails import accepted_fix, advice_is_safe, checked_question
from k8s_troubleshooter.tools.kubernetes import TOOL_NAMES, build_tools

AGENT_NAME = "k8s-troubleshooter"


def investigate(question: str, *, model=None, step_limit: int | None = None) -> dict:
    question = checked_question(question)
    run_id = uuid.uuid4().hex
    chosen = model or build_model()
    name, provider = describe_model(chosen)
    graph = build_graph(chosen, step_limit)
    limit = step_limit if step_limit is not None else max_steps()
    root = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": AGENT_NAME,
        "gen_ai.conversation.id": run_id,
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": name,
        "k8s_troubleshooter.question": question,
        "k8s_troubleshooter.step_limit": limit,
        "k8s_troubleshooter.system_prompt": SYSTEM_PROMPT,
    }
    with tracing(run_id, f"invoke_agent {AGENT_NAME}", root) as run:
        event("run_started", question=question, model=name, provider=provider, step_limit=limit)
        try:
            state = graph.invoke(
                {"messages": [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=question)], "steps": 0},
                config={"recursion_limit": limit * 2 + 4},
            )
        except ModelError as exc:
            state = {"stopped_reason": "model_error", "messages": [AIMessage(content=f"The model call failed: {_root_message(exc)}")]}
        findings = state.get("findings")
        stopped = state.get("stopped_reason", "")
        detail = str(state["messages"][-1].content) if state.get("messages") else ""
        run.finish(
            stopped_reason=stopped,
            steps=state.get("steps", 0),
            fault_found=(findings or {}).get("fault_found"),
            fix_proposed=bool((findings or {}).get("proposed_fix")),
            findings=findings,
            detail=detail if stopped != "conclusion" else None,
        )
    result = {
        "run_id": run_id,
        "question": question,
        "findings": findings,
        "steps": state.get("steps", 0),
        "stopped_reason": stopped,
        "trace_id": run.trace_id,
        "trace_path": str(run.path),
        "root_span_id": run.span_id,
    }
    if stopped != "conclusion" and detail:
        result["detail"] = detail
    return result


def build_graph(model, step_limit: int | None):
    tools = build_tools()
    bound = model.bind_tools(tools)
    limit = step_limit if step_limit is not None else max_steps()
    name, provider = describe_model(model)
    max_tokens = getattr(model, "max_tokens", None)

    def call_model(state: AgentState) -> dict:
        if state.get("steps", 0) >= limit:
            stopped = Findings(fault_found=False, cause="Stopped because the step limit was reached.", next_action="No change. Ask about one app.")
            return {
                "steps": state.get("steps", 0),
                "stopped_reason": "iteration_limit",
                "findings": stopped.model_dump(),
                "messages": [AIMessage(content="Stopped because the step limit was reached.")],
            }
        with model_step(name, provider, max_tokens=max_tokens, input_messages=len(state["messages"])) as step:
            response = _one_tool_call(bound.invoke(state["messages"]))
            step.model_response(response)
        update = {"messages": [response], "steps": state.get("steps", 0) + 1}
        if not getattr(response, "tool_calls", None):
            with traced_step("validate_answer") as check:
                checks: list[dict] = []
                try:
                    findings = parse_findings(str(response.content), state["messages"], checks=checks)
                    update["findings"] = findings.model_dump()
                    update["stopped_reason"] = "conclusion"
                    outcome = _outcome(checks)
                except ValueError as exc:
                    update["findings"] = None
                    update["stopped_reason"] = "invalid_answer"
                    update["messages"] = [AIMessage(content=str(exc))]
                    outcome = "rejected"
                    check.fail("invalid_answer", str(exc))
                check.set("k8s_troubleshooter.outcome", outcome)
                check.set("k8s_troubleshooter.checks", checks)
                check.set("k8s_troubleshooter.evidence_from", [item["action"] for item in (update["findings"] or {}).get("evidence", [])])
                check.fields.update(outcome=outcome, failed_checks=[item["check"] for item in checks if not item["passed"]])
        return update

    def route(state: AgentState) -> str:
        if state.get("stopped_reason"):
            return "end"
        last = state["messages"][-1]
        return "tools" if getattr(last, "tool_calls", None) else "end"

    graph = StateGraph(AgentState)
    graph.add_node("model", call_model)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", route, {"tools": "tools", "end": END})
    graph.add_edge("tools", "model")
    return graph.compile()


def parse_findings(text: str, messages: list | None = None, checks: list | None = None) -> Findings:
    """The model's answer, checked: real tools, evidence from tools that ran, safe advice, and only the allowed fix.

    Each check is appended to `checks` as {"check", "passed"}, so the trace shows how the answer was accepted."""
    checks = checks if checks is not None else []

    def check(name: str, passed: bool) -> bool:
        checks.append({"check": name, "passed": passed})
        return passed

    start, end = text.find("{"), text.rfind("}")
    if not check("answer is JSON", 0 <= start < end):
        raise ValueError("model did not return the answer JSON")
    findings = Findings.model_validate(json.loads(text[start : end + 1]))
    check("answer has the required fields", True)
    unknown = [item.action for item in findings.evidence if item.action not in TOOL_NAMES]
    if not check("evidence cites only known tools", not unknown):
        raise ValueError(f"evidence cites an unknown action: {unknown[0]}")
    if messages is not None:
        ran = {message.name for message in messages if isinstance(message, ToolMessage)}
        if not check("a tool ran before the answer", bool(ran)):
            raise ValueError("no tool ran before the answer")
        missing = [item.action for item in findings.evidence if item.action not in ran]
        if not check("evidence cites only tools that ran", not missing):
            raise ValueError(f"evidence cites a tool that did not run: {missing[0]}")
    if not check("next action is plain words with nothing destructive", advice_is_safe(findings.next_action)):
        findings.next_action = "Blocked an unsafe next action. No change was made."
        findings.proposed_fix = None
        return findings
    if findings.proposed_fix is not None:
        try:
            accepted_fix(findings.proposed_fix.model_dump())
            check("proposed fix is the one allowed port change", True)
        except (SafetyError, ValueError):
            check("proposed fix is the one allowed port change", False)
            findings.proposed_fix = None
            findings.next_action = "Blocked a health-check change outside the one allowed port change."
    return findings


def _outcome(checks: list[dict]) -> str:
    failed = {item["check"] for item in checks if not item["passed"]}
    if "next action is plain words with nothing destructive" in failed:
        return "advice_blocked"
    if "proposed fix is the one allowed port change" in failed:
        return "fix_blocked"
    return "accepted"


def _one_tool_call(response: AIMessage) -> AIMessage:
    calls = list(getattr(response, "tool_calls", None) or [])
    if len(calls) <= 1:
        return response
    return response.model_copy(update={"tool_calls": calls[:1]})


def _root_message(exc: BaseException) -> str:
    """The SDK says "Connection error."; the reason, such as a certificate failure, is the first cause."""
    root = exc
    while root.__cause__ is not None:
        root = root.__cause__
    text = str(exc) if root is exc or str(root) in str(exc) else f"{exc} ({root})"
    return text[:300]
