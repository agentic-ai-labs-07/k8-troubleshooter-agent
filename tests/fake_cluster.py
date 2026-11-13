"""A stand-in cluster for the checks that do not need kind."""

from __future__ import annotations


def pod(namespace, name, app, *, ready, reason="", restarts=0, deleting=False):
    return {
        "namespace": namespace,
        "name": name,
        "app": app,
        "phase": "Running",
        "ready": ready,
        "reason": reason,
        "restarts": restarts,
        "deleting": deleting,
    }


def events(namespace, name, message):
    return {
        "namespace": namespace,
        "name": name,
        "events": [{"type": "Warning", "reason": "Failed", "message": message}],
    }


def setup(namespace, workload, container, *, ports=None, env=None, configmaps=None, readiness_port=None):
    return {
        "found": True,
        "namespace": namespace,
        "workload": workload,
        "containers": [
            {
                "name": container,
                "ports": ports or [],
                "env": env or {},
                "configmaps": configmaps or [],
                "readiness_port": readiness_port,
            }
        ],
    }


class FakeCluster:
    """Three unhealthy apps, each with a healthy neighbor."""

    def __init__(self):
        self.patched = None
        self.pods = [
            pod("crashing-app", "crashing-app-aaaa", "crashing-app", ready=True),
            pod("crashing-app", "crashing-app-7f8d9c", "crashing-app", ready=False, reason="CrashLoopBackOff", restarts=3),
            pod("crashing-app", "healthy-app-6b7c8d", "healthy-app", ready=True),
            pod("not-ready-app", "not-ready-app-old", "not-ready-app", ready=False, deleting=True),
            pod("not-ready-app", "not-ready-app-5c6d7e", "not-ready-app", ready=False),
            pod("not-ready-app", "healthy-app-4b5c6d", "healthy-app", ready=True),
            pod("missing-config-app", "missing-config-app-3a4b5c", "missing-config-app", ready=False, reason="CreateContainerConfigError"),
            pod("missing-config-app", "healthy-app-2f3a4b", "healthy-app", ready=True),
        ]
        self.events = {
            ("crashing-app", "crashing-app-7f8d9c"): events("crashing-app", "crashing-app-7f8d9c", "Back-off restarting failed container"),
            ("not-ready-app", "not-ready-app-5c6d7e"): events("not-ready-app", "not-ready-app-5c6d7e", "Readiness probe failed: connection refused"),
            ("missing-config-app", "missing-config-app-3a4b5c"): events("missing-config-app", "missing-config-app-3a4b5c", 'configmap "app-settings" not found'),
        }
        self.logs = {
            ("crashing-app", "crashing-app-7f8d9c"): {
                "namespace": "crashing-app",
                "pod": "crashing-app-7f8d9c",
                "logs": "unknown APP_MODE bogus, expected normal or debug",
            }
        }
        self.setups = {
            ("crashing-app", "crashing-app"): setup(
                "crashing-app", "crashing-app", "crashing-app", env={"APP_MODE": "bogus"}
            ),
            ("not-ready-app", "not-ready-app"): setup(
                "not-ready-app", "not-ready-app", "not-ready-app", ports=[8080], readiness_port=8081
            ),
            ("missing-config-app", "missing-config-app"): setup(
                "missing-config-app",
                "missing-config-app",
                "missing-config-app",
                ports=[8080],
                configmaps=[{"name": "app-settings", "found": False}],
            ),
        }

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr("k8s_troubleshooter.clients.kubernetes.list_pods", self.list_pods)
        monkeypatch.setattr("k8s_troubleshooter.clients.kubernetes.list_events", self.list_events)
        monkeypatch.setattr("k8s_troubleshooter.clients.kubernetes.read_logs", self.read_logs)
        monkeypatch.setattr("k8s_troubleshooter.clients.kubernetes.read_setup", self.read_setup)
        monkeypatch.setattr("k8s_troubleshooter.clients.kubernetes.patch_readiness_port", self.patch_readiness_port)

    def list_pods(self, namespace: str = "") -> dict:
        chosen = self.pods
        if namespace and namespace not in {"all", "*"}:
            chosen = [item for item in self.pods if item["namespace"] == namespace]
        return {"pods": chosen}

    def list_events(self, namespace: str, name: str) -> dict:
        return self.events.get((namespace, name), {"namespace": namespace, "name": name, "events": []})

    def read_logs(self, namespace: str, pod_name: str) -> dict:
        return self.logs.get(
            (namespace, pod_name),
            {"namespace": namespace, "pod": pod_name, "logs": ""},
        )

    def read_setup(self, namespace: str, workload: str) -> dict:
        found = self.setups.get((namespace, workload))
        if found is None:
            return {"found": False, "namespace": namespace, "workload": workload}
        return found

    def patch_readiness_port(self, namespace, deployment, container, from_port, to_port) -> dict:
        from k8s_troubleshooter.exceptions import SafetyError

        found = self.read_setup(namespace, deployment)
        chosen = next((item for item in found.get("containers", []) if item["name"] == container), None)
        if chosen is None or chosen.get("readiness_port") != from_port or to_port not in chosen.get("ports", []):
            raise SafetyError("proposal does not match the live health check")
        chosen["readiness_port"] = to_port
        for item in self.pods:
            if item["namespace"] == namespace and item["app"] == deployment and not item.get("deleting"):
                item["ready"] = True
                item["reason"] = ""
        self.patched = {
            "namespace": namespace,
            "deployment": deployment,
            "container": container,
            "from_port": from_port,
            "to_port": to_port,
        }
        return {"patched": True, **self.patched}
