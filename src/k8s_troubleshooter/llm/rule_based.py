"""The CI stand-in model: plain rules, no network. It chooses the next tool from facts already collected."""

from __future__ import annotations

import json
import re
import uuid

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from k8s_troubleshooter.tools.kubernetes import TOOL_NAMES

STOP_WORDS = {"why", "is", "the", "this", "service", "unhealthy", "app", "not", "what", "wrong", "with", "pod", "down"}
_TOOL_FIELD = {"get_events": "name", "get_pod_logs": "pod", "get_setup": "workload"}


class RuleBasedModel(BaseChatModel):
    tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "rules"

    def bind_tools(self, tools, **kwargs):
        bound = self.model_copy()
        bound.tool_names = [tool.name for tool in tools]
        return bound

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = next_message(list(messages), set(self.tool_names))
        return ChatResult(generations=[ChatGeneration(message=message)])


def next_message(messages: list[BaseMessage], allowed: set[str] | None = None) -> AIMessage:
    """Pick the next tool. `allowed` is the tool list bound to this model."""
    allowed = set(TOOL_NAMES) if allowed is None else allowed
    question = next((str(message.content) for message in reversed(messages) if isinstance(message, HumanMessage)), "")
    status = _tool_payload(messages, "get_pod_status")
    if status is None:
        return _allowed_call(allowed, "get_pod_status", {})
    failed = _failed("get_pod_status", status)
    if failed is not None:
        return failed
    chosen = _select_pod(question, status.get("pods", []))
    if chosen is None:
        return _answer(_no_match(question, status.get("pods", [])))
    events = _named_result(messages, "get_events", chosen["name"])
    if events is None:
        return _allowed_call(allowed, "get_events", {"namespace": chosen["namespace"], "name": chosen["name"]})
    failed = _failed("get_events", events)
    if failed is not None:
        return failed
    needs_logs = chosen["reason"] == "CrashLoopBackOff" or chosen["restarts"] > 0
    logs = _named_result(messages, "get_pod_logs", chosen["name"]) if needs_logs else None
    if needs_logs and logs is None:
        return _allowed_call(allowed, "get_pod_logs", {"namespace": chosen["namespace"], "pod": chosen["name"]})
    failed = _failed("get_pod_logs", logs or {})
    if failed is not None:
        return failed
    setup = _named_result(messages, "get_setup", chosen["app"]) if chosen.get("app") else {}
    if chosen.get("app") and setup is None:
        return _allowed_call(allowed, "get_setup", {"namespace": chosen["namespace"], "workload": chosen["app"]})
    failed = _failed("get_setup", setup or {})
    if failed is not None:
        return failed
    return _answer(_conclude(chosen, messages))


def _named_result(messages: list[BaseMessage], tool_name: str, value: str) -> dict | None:
    """Match the tool's own field exactly, so `app` never matches `app-2`."""
    field = _TOOL_FIELD.get(tool_name)
    if not value or field is None:
        return None
    for message in messages:
        if isinstance(message, ToolMessage) and message.name == tool_name:
            payload = _load(str(message.content))
            if payload.get(field) == value:
                return payload
    return None


def _select_pod(question: str, pods: list[dict]) -> dict | None:
    tokens = [token for token in re.findall(r"[a-z0-9-]+", question.lower()) if token not in STOP_WORDS and len(token) > 2]
    scored = [(score, pod) for pod in pods if (score := _score(pod, tokens))]
    if scored:
        best = max(score for score, _pod in scored)
        return _one_app([pod for score, pod in scored if score == best])
    return _one_app([pod for pod in pods if not pod["ready"] and not pod.get("deleting")])


def _one_app(pods: list[dict]) -> dict | None:
    if len({pod.get("app") for pod in pods}) != 1:
        return None
    unhealthy = [pod for pod in pods if not pod["ready"] and not pod.get("deleting")]
    pool = unhealthy or [pod for pod in pods if not pod.get("deleting")] or list(pods)
    return sorted(pool, key=lambda pod: (-int(pod.get("restarts") or 0), pod["name"]))[0]


def _score(pod: dict, tokens: list[str]) -> int:
    score = 0
    for token in tokens:
        if token == pod.get("app"):
            score += 10
        elif token in pod.get("app", "") or token in pod.get("name", ""):
            score += 4
    return score


def _conclude(pod: dict, messages: list[BaseMessage]) -> dict:
    evidence = [{
        "action": "get_pod_status",
        "fact": f"{pod['namespace']}/{pod['name']} ready={pod['ready']} reason={pod['reason'] or 'none'} restarts={pod['restarts']}",
    }]
    events = _named_result(messages, "get_events", pod["name"])
    logs = _named_result(messages, "get_pod_logs", pod["name"])
    setup = _named_result(messages, "get_setup", pod.get("app") or "")
    if events:
        evidence.append({"action": "get_events", "fact": "; ".join(item.get("message", "") for item in events.get("events", [])) or "no events"})
    if logs is not None:
        evidence.append({"action": "get_pod_logs", "fact": logs.get("logs") or "no logs"})
    if setup:
        evidence.append({"action": "get_setup", "fact": json.dumps(setup)})
    missing = _missing_config(setup)
    if missing:
        return _result(True, f"{pod['app']} cannot start because ConfigMap {missing} was not found.", evidence, f"Create ConfigMap {missing}. This agent will not create it.")
    crash = _crash(pod, logs or {}, setup or {})
    if crash:
        key, value = crash
        return _result(True, f"{pod['app']} is crash-looping because {key} is {value} and the app log rejects that value.", evidence, f"Change {key} on {pod['app']} to a value the app accepts. This agent will not change it.")
    mismatch = _port_mismatch(setup or {})
    if mismatch and not pod["ready"]:
        container, from_port, to_port = mismatch
        return _result(
            True,
            f"{pod['app']} is running but not ready because the health check uses port {from_port} and the app listens on {to_port}.",
            evidence,
            f"Change the health-check port from {from_port} to {to_port}. Expected effect: the check reaches the app and the pod becomes ready.",
            {"namespace": pod["namespace"], "deployment": pod["app"], "container": container, "from_port": from_port, "to_port": to_port, "expected_effect": "The health check reaches the app and the pod becomes ready."},
        )
    if pod["ready"]:
        return _result(False, f"No fault found for {pod['app']}. It is ready.", evidence, "No change.")
    return _result(True, f"{pod['app']} looks unhealthy, but the facts do not name a single cause.", evidence, "No change. Look at one app with a narrower question.")


def _missing_config(setup: dict | None) -> str:
    for container in (setup or {}).get("containers", []):
        for item in container.get("configmaps", []):
            if item.get("found") is False:
                return item["name"]
    return ""


def _crash(pod: dict, logs: dict, setup: dict) -> tuple[str, str] | None:
    if pod.get("reason") != "CrashLoopBackOff" and not pod.get("restarts"):
        return None
    text = logs.get("logs") or ""
    for container in setup.get("containers", []):
        for key, value in container.get("env", {}).items():
            if value and key in text and re.search(rf"(?<![A-Za-z0-9]){re.escape(str(value))}(?![A-Za-z0-9])", text):
                return key, str(value)
    return None


def _port_mismatch(setup: dict) -> tuple[str, int, int] | None:
    for container in setup.get("containers", []):
        port = container.get("readiness_port")
        ports = [item for item in container.get("ports") or [] if isinstance(item, int) and not isinstance(item, bool)]
        if isinstance(port, int) and not isinstance(port, bool) and ports and port not in ports:
            return container["name"], port, ports[0]
    return None


def _no_match(question: str, pods: list[dict]) -> dict:
    names = ", ".join(sorted({pod["app"] for pod in pods if not pod["ready"]}))
    if not names:
        return _result(False, "No fault found. Every pod is ready.", [], "No change.")
    return _result(False, f"No single app matched {question!r}. Unhealthy apps: {names}.", [{"action": "get_pod_status", "fact": names}], "Ask again with one app name. No change was made.")


def _failed(name: str, payload: dict) -> AIMessage | None:
    error = payload.get("error")
    if not error:
        return None
    return _answer(_result(False, f"Could not read the cluster: {error}", [{"action": name, "fact": error}], "No change."))


def _tool_payload(messages: list[BaseMessage], tool_name: str) -> dict | None:
    for message in reversed(messages):
        if isinstance(message, ToolMessage) and message.name == tool_name:
            return _load(str(message.content))
    return None


def _allowed_call(allowed: set[str], name: str, args: dict) -> AIMessage:
    if name not in allowed:
        return _answer(_result(False, f"The tool {name} is not available.", [], "No change."))
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": uuid.uuid4().hex[:12], "type": "tool_call"}])


def _answer(payload: dict) -> AIMessage:
    return AIMessage(content=json.dumps(payload))


def _result(fault_found: bool, cause: str, evidence: list[dict], next_action: str, proposed_fix=None) -> dict:
    return {"fault_found": fault_found, "cause": cause, "evidence": evidence, "next_action": next_action, "proposed_fix": proposed_fix}


def _load(text: str) -> dict:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {"error": text}
    return payload if isinstance(payload, dict) else {"value": payload}
