"""The three planted failures and the facts a right answer must contain."""

from __future__ import annotations

SCENARIOS = (
    {
        "id": "crashing-app",
        "namespace": "crashing-app",
        "app": "crashing-app",
        "settled": lambda pod: pod["restarts"] >= 1,
        "question": "Why is crashing-app unhealthy?",
        "cause_contains": ["APP_MODE", "bogus"],
        "evidence_actions": ["get_pod_logs"],
        "next_contains": ["APP_MODE"],
        "fix": None,
    },
    {
        "id": "not-ready-app",
        "namespace": "not-ready-app",
        "app": "not-ready-app",
        "settled": lambda pod: pod["phase"] == "Running" and not pod["ready"],
        "question": "Why is not-ready-app unhealthy?",
        "cause_contains": ["8081", "8080"],
        "evidence_actions": ["get_events", "get_setup"],
        "next_contains": ["8080"],
        "fix": {
            "namespace": "not-ready-app",
            "deployment": "not-ready-app",
            "container": "not-ready-app",
            "from_port": 8081,
            "to_port": 8080,
        },
    },
    {
        "id": "missing-config-app",
        "namespace": "missing-config-app",
        "app": "missing-config-app",
        "settled": lambda pod: pod["reason"] == "CreateContainerConfigError",
        "question": "Why is missing-config-app unhealthy?",
        "cause_contains": ["app-settings"],
        "evidence_actions": ["get_events"],
        "next_contains": ["app-settings"],
        "fix": None,
    },
)
