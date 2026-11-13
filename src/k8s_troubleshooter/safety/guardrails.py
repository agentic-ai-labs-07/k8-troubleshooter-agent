"""Checks on what goes into the agent and what comes out of it."""

from __future__ import annotations

import re

from k8s_troubleshooter.agent.schemas import ProposedFix
from k8s_troubleshooter.exceptions import SafetyError

DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
MAX_QUESTION_CHARS = 500
MAX_ADVICE_CHARS = 400
# Advice is read by a person. A command in it could be copied from text planted in a log.
COMMAND_IN_ADVICE = re.compile(
    r"`|\$\(|&&|[;|]"
    r"|\b(kubectl|helm|docker|curl|wget|bash|sh|zsh|sudo|rm|chmod|chown|python|eval|exec|ssh)\b",
    re.IGNORECASE,
)
DESTRUCTIVE_ADVICE = re.compile(r"\b(delete|destroy|drop|wipe|purge|truncate|drain|cordon)\b", re.IGNORECASE)


def checked_question(text: str) -> str:
    question = text.strip()
    if not question:
        raise ValueError("question is empty")
    if len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"question is longer than {MAX_QUESTION_CHARS} characters")
    return question


def dns_label(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not DNS_LABEL.fullmatch(value) or len(value) > 63:
        raise SafetyError(f"{field_name} is not a Kubernetes name")
    return value


def guard_namespace(value: str) -> str:
    name = dns_label(value, "namespace")
    if name.startswith("kube-"):
        raise SafetyError("system namespaces are out of scope")
    return name


def optional_namespace(value: str) -> str:
    if value in {"", "all", "*"}:
        return ""
    return guard_namespace(value)


def port_number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise SafetyError("port is out of range")
    return value


def advice_is_safe(text: str) -> bool:
    """Plain words only: no command, no shell syntax, nothing destructive."""
    text = text or ""
    return len(text) <= MAX_ADVICE_CHARS and not COMMAND_IN_ADVICE.search(text) and not DESTRUCTIVE_ADVICE.search(text)


def accepted_fix(payload: dict) -> dict:
    """The one allowed change: a readiness port, with valid names, that actually changes."""
    try:
        fix = ProposedFix.model_validate(payload)
    except ValueError as exc:
        raise SafetyError("proposal is not the allowed port change") from exc
    guard_namespace(fix.namespace)
    dns_label(fix.deployment, "deployment")
    dns_label(fix.container, "container")
    port_number(fix.from_port)
    port_number(fix.to_port)
    if fix.from_port == fix.to_port:
        raise SafetyError("port change does not change the port")
    return fix.model_dump()
