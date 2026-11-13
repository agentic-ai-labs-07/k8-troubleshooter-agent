"""The port change runs once, and only when the yes matches the live port."""

from __future__ import annotations

import json

import pytest

from k8s_troubleshooter.agent.graph import investigate
from k8s_troubleshooter.agent.remediation import approve, save_proposal
from k8s_troubleshooter.evals.graders import grade_fix
from k8s_troubleshooter.exceptions import SafetyError
from k8s_troubleshooter.observability.trace_file import read_spans
from tests.fake_cluster import FakeCluster


def test_yes_applies_the_port_once(monkeypatch):
    fake = FakeCluster()
    fake.install(monkeypatch)
    result = investigate("Why is not-ready-app unhealthy?")
    assert fake.patched is None
    proposal_id = save_proposal(result["findings"], result["run_id"])
    report = approve(proposal_id)
    assert grade_fix(report) == []
    assert fake.patched == {
        "namespace": "not-ready-app",
        "deployment": "not-ready-app",
        "container": "not-ready-app",
        "from_port": 8081,
        "to_port": 8080,
    }
    with pytest.raises(SafetyError, match="not waiting"):
        approve(proposal_id)


def test_the_change_and_the_recheck_are_traced(monkeypatch):
    FakeCluster().install(monkeypatch)
    result = investigate("Why is not-ready-app unhealthy?")
    report = approve(save_proposal(result["findings"], result["run_id"], result["trace_id"], result["root_span_id"]))
    spans = read_spans(report["trace_path"])
    names = [span["name"] for span in spans]
    assert names == ["approve_fix", "execute_tool patch_readiness_port", "recheck"]
    root, patch, recheck = spans
    assert root["links"] == [{"trace_id": result["trace_id"], "span_id": result["root_span_id"]}]
    assert root["attributes"]["k8s_troubleshooter.investigation_run_id"] == result["run_id"]
    assert json.loads(patch["attributes"]["gen_ai.tool.call.arguments"])["to_port"] == 8080
    assert json.loads(patch["attributes"]["gen_ai.tool.call.result"])["patched"] is True
    assert recheck["attributes"]["k8s_troubleshooter.resolved"] is True
    assert all(span["end"] >= span["start"] for span in spans)
    assert {span["parent_id"] for span in spans[1:]} == {root["span_id"]}


def test_yes_refuses_a_different_port(monkeypatch):
    fake = FakeCluster()
    fake.install(monkeypatch)
    proposal_id = "a" * 32
    _write_proposal(
        proposal_id,
        {**_pending(proposal_id), "from_port": 9999},
    )
    with pytest.raises(SafetyError, match="does not match"):
        approve(proposal_id)
    assert fake.patched is None


def test_yes_refuses_a_system_namespace(monkeypatch):
    fake = FakeCluster()
    fake.install(monkeypatch)
    proposal_id = "d" * 32
    _write_proposal(proposal_id, {**_pending(proposal_id), "namespace": "kube-system"})
    with pytest.raises(SafetyError, match="system"):
        approve(proposal_id)
    assert fake.patched is None


def test_unknown_proposal_is_rejected():
    with pytest.raises(SafetyError, match="unknown"):
        approve("nope")
    with pytest.raises(SafetyError, match="unknown"):
        approve("b" * 32)


def _write_proposal(proposal_id: str, record: dict) -> None:
    from k8s_troubleshooter.config import trace_dir

    path = trace_dir() / "proposals" / f"{proposal_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


def _pending(proposal_id: str) -> dict:
    return {
        "proposal_id": proposal_id,
        "run_id": "c" * 32,
        "status": "pending",
        "namespace": "not-ready-app",
        "deployment": "not-ready-app",
        "container": "not-ready-app",
        "from_port": 8081,
        "to_port": 8080,
        "expected_effect": "the pod becomes ready",
        "change": "Change the health-check port.",
    }
