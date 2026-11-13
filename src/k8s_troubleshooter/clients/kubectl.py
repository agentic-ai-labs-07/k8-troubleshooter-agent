"""The one place that runs kubectl: a fixed argument list, no shell, and a time limit."""

from __future__ import annotations

import json
import subprocess

from k8s_troubleshooter.config import kube_server, kubeconfig_path
from k8s_troubleshooter.exceptions import CommandError, SafetyError

FORBIDDEN = (";", "|", "&", "`", "$", "\n", "\x00")
TIMEOUT_SECONDS = 30


def command(args: list[str]) -> list[str]:
    kubeconfig = kubeconfig_path()
    if not kubeconfig.is_file():
        raise SafetyError(f"kind kubeconfig not found: {kubeconfig}")
    for arg in args:
        if not isinstance(arg, str) or any(token in arg for token in FORBIDDEN):
            raise SafetyError("kubectl argument is not allowed")
    server = ["--server", kube_server()] if kube_server() else []
    return ["kubectl", "--kubeconfig", str(kubeconfig), *server, *args]


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(command(args), check=False, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise CommandError("kubectl timed out") from exc
    if completed.returncode != 0:
        raise CommandError(_reason(completed.stderr) or completed.stdout.strip() or "kubectl failed")
    return completed


def _reason(stderr: str) -> str:
    """kubectl repeats client-go retry lines before its own `error:` line. Keep only that line when there is one."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("error:")]
    return errors[-1] if errors else "\n".join(lines)


def run_json(args: list[str]) -> dict:
    return json.loads(run([*args, "-o", "json"]).stdout)
