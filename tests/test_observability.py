"""Traces are standard OTLP with GenAI names; logs are one event per line, tied to the trace, with secrets hidden."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from k8s_troubleshooter.agent.graph import investigate
from k8s_troubleshooter.cli import main
from k8s_troubleshooter.exceptions import CommandError
from k8s_troubleshooter.observability.trace_file import read_spans
from tests.fake_cluster import FakeCluster

CLIENT = "k8s_troubleshooter.clients.kubernetes"
FAKE_TOKEN = "sk-" + "x" * 24


def test_each_line_is_an_otlp_export_request(monkeypatch):
    FakeCluster().install(monkeypatch)
    result = investigate("Why is crashing-app unhealthy?")
    lines = [json.loads(line) for line in Path(result["trace_path"]).read_text(encoding="utf-8").splitlines()]

    for request in lines:
        resource = request["resourceSpans"][0]
        attributes = {item["key"]: item["value"] for item in resource["resource"]["attributes"]}
        assert attributes["service.name"] == {"stringValue": "k8s-troubleshooter"}
        span = resource["scopeSpans"][0]["spans"][0]
        assert re.fullmatch(r"[0-9a-f]{32}", span["traceId"]) and re.fullmatch(r"[0-9a-f]{16}", span["spanId"])
        assert span["startTimeUnixNano"].isdigit() and isinstance(span["kind"], int)
        assert span["status"]["code"] in (0, 1, 2)


def test_spans_use_the_genai_names(monkeypatch):
    FakeCluster().install(monkeypatch)
    result = investigate("Why is crashing-app unhealthy?")
    spans = read_spans(result["trace_path"])
    root, steps = spans[0], spans[1:]

    assert root["attributes"]["gen_ai.operation.name"] == "invoke_agent"
    assert root["attributes"]["gen_ai.conversation.id"] == result["run_id"]
    chats = [span for span in steps if span["attributes"].get("gen_ai.operation.name") == "chat"]
    tools = [span for span in steps if span["attributes"].get("gen_ai.operation.name") == "execute_tool"]
    assert chats and all(span["name"] == "chat rules" and span["kind"] == "client" for span in chats)
    assert [span["name"] for span in tools][:2] == ["execute_tool get_pod_status", "execute_tool get_events"]
    assert all("gen_ai.tool.call.arguments" in span["attributes"] and "gen_ai.tool.call.result" in span["attributes"] for span in tools)
    decision = json.loads(chats[0]["attributes"]["gen_ai.output.messages"])[0]["parts"][0]
    assert decision["type"] == "tool_call" and decision["name"] == "get_pod_status"
    assert all(span["parent_id"] == root["span_id"] for span in steps)


def test_a_failed_read_marks_its_span_as_an_error(monkeypatch, caplog):
    def list_pods(namespace=""):
        raise CommandError("kind kubeconfig not found: missing")

    monkeypatch.setattr(f"{CLIENT}.list_pods", list_pods)
    caplog.set_level(logging.INFO, logger="k8s_troubleshooter")
    result = investigate("Why is crashing-app unhealthy?")
    failed = next(span for span in read_spans(result["trace_path"]) if span["name"] == "execute_tool get_pod_status")

    assert failed["status"] == "error"
    assert failed["attributes"]["error.type"] == "CommandError"
    assert "kubeconfig not found" in failed["status_message"]
    finished = next(record.event_fields for record in caplog.records if record.getMessage() == "run_finished")
    assert finished["tool_errors"] == 1


def test_json_logs_are_one_event_per_line(monkeypatch, capsys):
    FakeCluster().install(monkeypatch)
    monkeypatch.setenv("LOG_FORMAT", "json")
    assert main(["investigate", "Why is crashing-app unhealthy?"]) == 0
    captured = capsys.readouterr()
    answer = json.loads(captured.out)
    lines = [json.loads(line) for line in captured.err.splitlines() if line.strip()]

    assert [line["event"] for line in lines][0] == "run_started"
    assert all(line["run_id"] == answer["run_id"] for line in lines if line["event"] != "fix_proposed")
    tool_lines = [line for line in lines if line["event"] == "tool_call"]
    assert tool_lines and all(line["trace_id"] == answer["trace_id"] and line["span_id"] for line in tool_lines)
    finished = next(line for line in lines if line["event"] == "run_finished")
    assert finished["stopped_reason"] == "conclusion" and finished["latency_ms"] >= 0


def test_logs_hide_secrets(monkeypatch, capsys):
    def list_pods(namespace=""):
        raise CommandError(f"request failed: password=hunter2 token {FAKE_TOKEN}")

    monkeypatch.setattr(f"{CLIENT}.list_pods", list_pods)
    monkeypatch.setenv("LOG_FORMAT", "json")
    main(["investigate", "Why is crashing-app unhealthy?"])
    captured = capsys.readouterr()

    assert "hunter2" not in captured.err and FAKE_TOKEN not in captured.err
    assert "hunter2" not in captured.out and FAKE_TOKEN not in captured.out
    trace = Path(json.loads(captured.out)["trace_path"]).read_text(encoding="utf-8")
    assert "hunter2" not in trace and FAKE_TOKEN not in trace


def test_text_logs_are_readable(monkeypatch, capsys):
    FakeCluster().install(monkeypatch)
    assert main(["investigate", "Why is crashing-app unhealthy?"]) == 0
    err = capsys.readouterr().err

    assert re.search(r"INFO\s+run_started\s+run_id=[0-9a-f]{32} question=\"Why is crashing-app unhealthy\?\"", err)
    assert re.search(r"INFO\s+tool_call\s+.*tool=get_pod_logs.*status=ok", err)
    assert re.search(r"INFO\s+run_finished\s+.*stopped_reason=conclusion", err)
