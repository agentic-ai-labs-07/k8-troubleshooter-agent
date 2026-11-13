"""Run the same checks against kind. Skipped when the kind cluster is not running; fails in CI instead."""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

from k8s_troubleshooter.agent.graph import investigate
from k8s_troubleshooter.agent.remediation import approve, save_proposal
from k8s_troubleshooter.cli import apply_scenario, delete_scenario, wait_for_app
from k8s_troubleshooter.clients.kubectl import run
from k8s_troubleshooter.config import max_steps, project_root
from k8s_troubleshooter.evals.graders import grade, grade_fix
from k8s_troubleshooter.evals.scenarios import SCENARIOS
from k8s_troubleshooter.exceptions import CommandError, SafetyError


@pytest.mark.cluster
def test_known_failures_on_kind(monkeypatch):
    names = [scenario["id"] for scenario in SCENARIOS]
    with _as_admin(monkeypatch):
        for name in names:
            apply_scenario(name)
    try:
        for scenario in SCENARIOS:
            wait_for_app(scenario["namespace"], scenario["app"], scenario["settled"])
        proposal_id = None
        for scenario in SCENARIOS:
            result = investigate(scenario["question"])
            assert grade(result, scenario, max_steps=max_steps()) == []
            if scenario["fix"]:
                proposal_id = save_proposal(result["findings"], result["run_id"])
        report = approve(proposal_id)
        assert grade_fix(report) == []
        with pytest.raises(SafetyError):
            approve(proposal_id)
    finally:
        with _as_admin(monkeypatch):
            for name in names:
                delete_scenario(name)


@pytest.mark.cluster
def test_agent_login_is_limited_by_kubernetes():
    allowed = (
        ["list", "pods", "-A"],
        ["get", "pods", "-n", "crashing-app"],
        ["get", "pods", "--subresource=log", "-n", "crashing-app"],
        ["get", "configmaps", "-n", "missing-config-app"],
        ["patch", "deployment/not-ready-app", "-n", "not-ready-app"],
    )
    for request in allowed:
        run(["auth", "can-i", *request])
    denied = (
        ["patch", "deployment/healthy-app", "-n", "not-ready-app"],
        ["patch", "deployment/not-ready-app", "-n", "crashing-app"],
        ["get", "pods", "-n", "kube-system"],
        ["get", "pods", "--subresource=log", "-n", "kube-system"],
        ["get", "configmaps", "-n", "kube-system"],
        ["get", "deployments", "-n", "kube-system"],
        ["list", "events", "-n", "kube-system"],
        ["delete", "pods", "-A"],
        ["get", "secrets", "-A"],
        ["create", "pods/exec", "-n", "not-ready-app"],
        ["create", "namespaces"],
    )
    for request in denied:
        with pytest.raises(CommandError):
            run(["auth", "can-i", *request])


@contextmanager
def _as_admin(monkeypatch):
    agent = os.environ["KUBECONFIG"]
    monkeypatch.setenv("KUBECONFIG", str(project_root() / ".kube" / "admin"))
    try:
        yield
    finally:
        monkeypatch.setenv("KUBECONFIG", agent)
