"""The four tools the model may call. Each one reads; none writes."""

from __future__ import annotations

import json

from langchain_core.tools import tool

from k8s_troubleshooter.clients import kubernetes as cluster
from k8s_troubleshooter.exceptions import CommandError, SafetyError
from k8s_troubleshooter.observability.tracing import tool_step
from k8s_troubleshooter.safety.redaction import redact_payload, redact_text

TOOL_NAMES = ("get_pod_status", "get_events", "get_pod_logs", "get_setup")
MAX_PODS = 100


def build_tools() -> list:
    return [get_pod_status, get_events, get_pod_logs, get_setup]


@tool
def get_pod_status() -> str:
    """Which pods are running and ready, in every namespace. Use it to find the app's namespace."""
    return _call("get_pod_status", lambda: _capped(cluster.list_pods("")), {"namespace": "all"})


@tool
def get_events(namespace: str, name: str) -> str:
    """What Kubernetes recorded for one pod."""
    return _call("get_events", lambda: cluster.list_events(namespace, name), {"namespace": namespace, "name": name})


@tool
def get_pod_logs(namespace: str, pod: str) -> str:
    """What the app printed, including the previous crash."""
    return _call("get_pod_logs", lambda: cluster.read_logs(namespace, pod), {"namespace": namespace, "pod": pod})


@tool
def get_setup(namespace: str, workload: str) -> str:
    """The Deployment spec: containers, ports, health check, and the ConfigMaps they load. The app label is the Deployment name."""
    return _call("get_setup", lambda: cluster.read_setup(namespace, workload), {"namespace": namespace, "workload": workload})


def _call(name: str, read, details: dict) -> str:
    """Run one read, hide secrets in the whole result, and trace it."""
    with tool_step(name, details) as step:
        try:
            text = json.dumps(redact_payload(read()))
        except (CommandError, OSError, SafetyError) as exc:
            error = redact_text(str(exc))
            text = json.dumps({"error": error})
            step.fail(type(exc).__name__, error)
        step.tool_result(text)
        return text


def _capped(listing: dict) -> dict:
    """Unhealthy pods first, so a big cluster cannot flood the model."""
    pods = sorted(listing["pods"], key=lambda pod: pod["ready"])
    if len(pods) <= MAX_PODS:
        return {"pods": pods}
    return {"pods": pods[:MAX_PODS], "omitted_healthy_or_extra": len(pods) - MAX_PODS}
