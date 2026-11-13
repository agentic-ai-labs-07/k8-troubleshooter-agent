"""Settings come from the environment."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    override = os.environ.get("K8S_TROUBLESHOOTER_ROOT")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "scenarios" / "crashing-app").is_dir():
            return parent
    return Path.cwd()


def kubeconfig_path() -> Path:
    """Kind config for this project. Never the machine's default cluster."""
    override = os.environ.get("KUBECONFIG")
    if override:
        return Path(override)
    return project_root() / ".kube" / "config"


def kube_server() -> str:
    """API address for this process. Containers reach kind by name, not by 127.0.0.1."""
    return os.environ.get("KUBE_SERVER", "").strip()


def trace_dir() -> Path:
    override = os.environ.get("TRACE_DIR")
    if override:
        return Path(override)
    return project_root() / "traces"


def log_file() -> Path:
    """Every command's log lines, from both containers. Next to the traces, so it is already mounted."""
    override = os.environ.get("LOG_FILE")
    if override:
        return Path(override)
    return trace_dir() / "logs" / "k8s-troubleshooter.jsonl"


def max_steps() -> int:
    return max(1, int(os.environ.get("AGENT_MAX_STEPS", "8")))


def max_tokens() -> int:
    return max(1, int(os.environ.get("AGENT_MAX_TOKENS", "1024")))


def log_level() -> str:
    """INFO shows each step. DEBUG adds what each step returned."""
    return os.environ.get("LOG_LEVEL", "").strip().upper() or "INFO"


def log_format() -> str:
    """`text` (default) for people, `json` for one JSON object per line."""
    return "json" if os.environ.get("LOG_FORMAT", "").strip().lower() == "json" else "text"


def model_mode() -> str:
    """`live` (default) calls the configured model. `rules` is the rule-based CI stand-in."""
    return os.environ.get("AGENT_MODEL", "").strip().lower() or "live"


def openai_api_key() -> str:
    """From OPENAI_API_KEY, or from the file at OPENAI_API_KEY_FILE (the Compose secret)."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    path = os.environ.get("OPENAI_API_KEY_FILE", "").strip()
    if key or not path:
        return key
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
