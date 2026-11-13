"""The one approved fix: save the proposal, apply it after a yes, then check again with fresh reads."""

from __future__ import annotations

import json
import os
import re
import time
import uuid

from k8s_troubleshooter.clients import kubernetes as cluster
from k8s_troubleshooter.config import trace_dir
from k8s_troubleshooter.exceptions import CommandError, SafetyError
from k8s_troubleshooter.observability.logs import event
from k8s_troubleshooter.observability.tracing import tool_step, traced_step, tracing
from k8s_troubleshooter.safety.guardrails import accepted_fix
from k8s_troubleshooter.safety.redaction import redact_payload

_PROPOSAL_ID = re.compile(r"^[a-f0-9]{32}$")


def save_proposal(findings: dict, run_id: str, trace_id: str | None = None, span_id: str | None = None) -> str | None:
    proposed = findings.get("proposed_fix")
    if not proposed:
        return None
    proposal_id = uuid.uuid4().hex
    proposed = accepted_fix(proposed)
    record = {
        **proposed,
        "proposal_id": proposal_id,
        "run_id": run_id,
        "status": "pending",
        "change": (
            f"Change the health-check port on {proposed['namespace']}/{proposed['deployment']} "
            f"container {proposed['container']} from {proposed['from_port']} to {proposed['to_port']}."
        ),
    }
    if trace_id and span_id:
        record["trace"] = {"trace_id": trace_id, "span_id": span_id}
    path = trace_dir() / "proposals" / f"{proposal_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return proposal_id


def approve(proposal_id: str) -> dict:
    if not _PROPOSAL_ID.fullmatch(proposal_id):
        raise SafetyError("unknown proposal")
    path = trace_dir() / "proposals" / f"{proposal_id}.json"
    if not path.is_file():
        raise SafetyError("unknown proposal")
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("status") != "pending":
        raise SafetyError("proposal is not waiting for a yes")
    checked = accepted_fix(record)
    root = {
        "k8s_troubleshooter.proposal_id": proposal_id,
        "k8s_troubleshooter.change": record["change"],
        "k8s_troubleshooter.expected_effect": record.get("expected_effect"),
        "k8s_troubleshooter.investigation_run_id": record.get("run_id"),
    }
    with tracing(f"approve-{proposal_id}", "approve_fix", root, link=record.get("trace")) as session:
        event("approve_started", proposal_id=proposal_id, change=record["change"], investigation_run_id=record.get("run_id"))
        with tool_step("patch_readiness_port", checked, approved=True) as step:
            patched = cluster.patch_readiness_port(
                checked["namespace"], checked["deployment"], checked["container"], checked["from_port"], checked["to_port"]
            )
            step.tool_result(json.dumps(patched))
        record["status"] = "applied"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        with traced_step("recheck", namespace=record["namespace"], deployment=record["deployment"], expected_port=record["to_port"]) as step:
            try:
                seen = _watch(record)
            except (CommandError, SafetyError, OSError) as exc:
                seen = {
                    "resolved": False,
                    "readiness_port": None,
                    "fresh_evidence": [{"action": "get_pod_status", "fact": str(exc)}],
                }
                step.fail(type(exc).__name__, str(exc))
            seen["fresh_evidence"] = redact_payload(seen["fresh_evidence"])
            step.set("k8s_troubleshooter.resolved", seen["resolved"])
            step.set("k8s_troubleshooter.readiness_port", seen["readiness_port"])
            step.set("k8s_troubleshooter.fresh_evidence", seen["fresh_evidence"])
            step.fields.update(resolved=seen["resolved"], readiness_port=seen["readiness_port"])
        session.finish(stopped_reason="recheck", resolved=seen["resolved"], readiness_port=seen["readiness_port"])
    return {
        "proposal_id": proposal_id,
        "applied": True,
        "resolved": seen["resolved"],
        "readiness_port": seen["readiness_port"],
        "change": record["change"],
        "expected_effect": record["expected_effect"],
        "fresh_evidence": seen["fresh_evidence"],
        "trace_path": str(session.path),
    }


def _watch(record: dict) -> dict:
    deadline = time.monotonic() + max(0.0, float(os.environ.get("AGENT_APPROVE_WAIT", "120")))
    seen = _snapshot(record)
    while not seen["resolved"] and time.monotonic() < deadline:
        time.sleep(min(2, max(0, deadline - time.monotonic())))
        seen = _snapshot(record)
    return seen


def _snapshot(record: dict) -> dict:
    pods = [pod for pod in cluster.list_pods(record["namespace"])["pods"] if pod["app"] == record["deployment"] and not pod.get("deleting")]
    setup = cluster.read_setup(record["namespace"], record["deployment"])
    container = next((item for item in setup.get("containers", []) if item["name"] == record["container"]), None)
    ready = bool(pods) and all(pod["ready"] for pod in pods)
    port = None if container is None else container.get("readiness_port")
    return {
        "resolved": ready and port == record["to_port"],
        "readiness_port": port,
        "fresh_evidence": [
            {"action": "get_pod_status", "fact": json.dumps({"ready": ready, "pods": pods})},
            {"action": "get_setup", "fact": json.dumps(setup)},
        ],
    }
