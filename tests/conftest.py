"""Keep unit tests off the machine's cluster and off a live model."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from k8s_troubleshooter.config import project_root


@pytest.fixture(autouse=True)
def isolate_local_run(request, monkeypatch, tmp_path):
    monkeypatch.setenv("TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setenv("AGENT_MODEL", "rules")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY_FILE", raising=False)
    monkeypatch.delenv("KUBE_SERVER", raising=False)
    if "cluster" not in request.keywords:
        monkeypatch.setenv("KUBECONFIG", str(tmp_path / "missing-kubeconfig"))
        monkeypatch.setenv("AGENT_APPROVE_WAIT", "0")
        return
    kubeconfig = project_root() / ".kube" / "config"
    if not kubeconfig.is_file() or not (project_root() / ".kube" / "admin").is_file():
        _unavailable("kind kubeconfigs are missing. Run scripts/setup-access.sh", "kind kubeconfigs are missing")
    problem = _cluster_problem(project_root() / ".kube" / "admin")
    if problem:
        _unavailable(f"kind cluster is not usable: {problem}", problem)
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))


def _cluster_problem(admin: Path) -> str:
    """Why the kind cluster cannot be used right now, or "" when its API server answers."""
    try:
        done = subprocess.run(
            ["kubectl", "--kubeconfig", str(admin), "get", "--raw", "/readyz"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"kubectl could not run ({exc})"
    if done.returncode != 0:
        return "the API server did not answer. Start it with `kind create cluster --config kind.yaml`"
    return ""


def _unavailable(ci_message: str, skip_message: str) -> None:
    """In CI a missing cluster is a failure. Locally the cluster tests are skipped, with the reason."""
    if os.environ.get("CI"):
        pytest.fail(ci_message)
    pytest.skip(skip_message)
