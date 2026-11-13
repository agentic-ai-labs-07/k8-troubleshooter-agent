"""Cluster reads and the one patch, as small summaries instead of raw Kubernetes objects."""

from __future__ import annotations

from k8s_troubleshooter.clients.kubectl import run, run_json
from k8s_troubleshooter.exceptions import CommandError, SafetyError
from k8s_troubleshooter.safety.guardrails import dns_label, guard_namespace, optional_namespace, port_number
from k8s_troubleshooter.safety.redaction import redact_text, visible_value

MAX_LOG_CHARS = 8000


def list_pods(namespace: str = "") -> dict:
    namespace = optional_namespace(namespace)
    args = ["get", "pods", "-A"] if not namespace else ["get", "pods", "-n", namespace]
    pods = [
        _pod_summary(item)
        for item in run_json(args).get("items", [])
        if not str((item.get("metadata") or {}).get("namespace", "")).startswith("kube-")
    ]
    return {"pods": pods}


def list_events(namespace: str, name: str) -> dict:
    namespace, name = guard_namespace(namespace), dns_label(name, "name")
    raw = run_json(["get", "events", "-n", namespace, "--field-selector", f"involvedObject.name={name}"])
    newest = sorted(raw.get("items", []), key=lambda item: item.get("lastTimestamp") or "")[-20:]
    events = [{"reason": item.get("reason", ""), "message": item.get("message", "")} for item in newest]
    return {"namespace": namespace, "name": name, "events": events}


def read_logs(namespace: str, pod: str) -> dict:
    namespace, pod = guard_namespace(namespace), dns_label(pod, "pod")
    text = "\n".join(part for part in (_logs(namespace, pod, previous=True), _logs(namespace, pod, previous=False)) if part)
    return {"namespace": namespace, "pod": pod, "logs": redact_text(text.strip())[-MAX_LOG_CHARS:]}


def read_setup(namespace: str, workload: str) -> dict:
    namespace, workload = guard_namespace(namespace), dns_label(workload, "workload")
    try:
        deployment = run_json(["get", "deployment", workload, "-n", namespace])
    except CommandError:
        return {"found": False, "namespace": namespace, "workload": workload}
    spec = ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
    return {
        "found": True,
        "namespace": namespace,
        "workload": workload,
        "containers": [_container_summary(namespace, item) for item in spec.get("containers") or []],
    }


def patch_readiness_port(namespace: str, deployment: str, container: str, from_port: int, to_port: int) -> dict:
    """Change only the health-check port, and only to a port the app already uses."""
    namespace, deployment = guard_namespace(namespace), dns_label(deployment, "deployment")
    container, from_port, to_port = dns_label(container, "container"), port_number(from_port), port_number(to_port)
    setup = read_setup(namespace, deployment)
    if not setup.get("found"):
        raise SafetyError("deployment not found")
    matches = [(index, item) for index, item in enumerate(setup["containers"]) if item["name"] == container]
    if not matches:
        raise SafetyError("container not found")
    index, chosen = matches[0]
    if chosen["readiness_port"] != from_port or to_port not in chosen["ports"]:
        raise SafetyError("proposal does not match the live health check")
    patch = (
        '[{"op":"replace","path":'
        f'"/spec/template/spec/containers/{index}/readinessProbe/httpGet/port","value":{to_port}}}]'
    )
    run(["patch", "deployment", deployment, "-n", namespace, "--type=json", "-p", patch])
    return {"patched": True, "namespace": namespace, "deployment": deployment, "port": to_port}


def _pod_summary(pod: dict) -> dict:
    meta, status = pod.get("metadata") or {}, pod.get("status") or {}
    containers = status.get("containerStatuses") or []
    waiting = [((item.get("state") or {}).get("waiting") or {}).get("reason", "") for item in containers]
    ready = any(item.get("type") == "Ready" and item.get("status") == "True" for item in status.get("conditions") or [])
    return {
        "namespace": meta.get("namespace", ""),
        "name": meta.get("name", ""),
        "app": (meta.get("labels") or {}).get("app", ""),
        "phase": status.get("phase", ""),
        "ready": ready,
        "reason": next((item for item in waiting if item), status.get("reason") or ""),
        "restarts": sum(item.get("restartCount", 0) for item in containers),
        "deleting": bool(meta.get("deletionTimestamp")),
    }


def _container_summary(namespace: str, container: dict) -> dict:
    probe = ((container.get("readinessProbe") or {}).get("httpGet") or {})
    configmaps = []
    for source in container.get("envFrom") or []:
        name = (source.get("configMapRef") or {}).get("name")
        if name:
            configmaps.append({"name": name, "found": _configmap_exists(namespace, name)})
    ports = [
        item["containerPort"]
        for item in container.get("ports") or []
        if isinstance(item.get("containerPort"), int) and not isinstance(item.get("containerPort"), bool)
    ]
    return {
        "name": container.get("name", ""),
        "ports": ports,
        "env": {item["name"]: _env_value(item) for item in container.get("env") or [] if item.get("name")},
        "configmaps": configmaps,
        "readiness_port": probe.get("port"),
    }


def _env_value(item: dict) -> str:
    if "valueFrom" in item:
        return "<from Secret>" if "secretKeyRef" in item["valueFrom"] else "<from reference>"
    return visible_value(item["name"], str(item.get("value", "")))


def _configmap_exists(namespace: str, name: str) -> bool:
    try:
        run(["get", "configmap", name, "-n", namespace, "-o", "name"])
    except CommandError:
        return False
    return True


def _logs(namespace: str, pod: str, *, previous: bool) -> str:
    args = ["logs", pod, "-n", namespace, "--tail=80"]
    if previous:
        args.append("--previous")
    try:
        return run(args).stdout.strip()
    except CommandError as exc:
        if "forbidden" in str(exc).lower():
            raise
        return ""
