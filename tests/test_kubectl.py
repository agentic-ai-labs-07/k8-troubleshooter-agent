"""A kubectl failure reads as one clear reason."""

from __future__ import annotations

import pytest

from k8s_troubleshooter.clients import kubectl
from k8s_troubleshooter.exceptions import CommandError


def test_a_kubectl_failure_keeps_only_its_error_line(monkeypatch, tmp_path):
    kubeconfig = tmp_path / "config"
    kubeconfig.write_text("apiVersion: v1\n", encoding="utf-8")
    monkeypatch.setenv("KUBECONFIG", str(kubeconfig))
    retries = 'E1111 08:41:54.588768 9 memcache.go:265] "Unhandled Error" err="couldn\'t get current server API group list"\n' * 3
    failed = kubectl.subprocess.CompletedProcess([], 1, "", retries + "error: You must be logged in to the server\n")
    monkeypatch.setattr(kubectl.subprocess, "run", lambda *args, **kwargs: failed)

    with pytest.raises(CommandError) as raised:
        kubectl.run(["get", "pods"])
    assert str(raised.value) == "error: You must be logged in to the server"
