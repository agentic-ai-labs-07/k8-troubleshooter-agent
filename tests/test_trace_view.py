"""The trace command turns a run into a report a person can read."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from k8s_troubleshooter.agent.graph import investigate
from k8s_troubleshooter.cli import main
from tests.fake_cluster import FakeCluster


def test_the_latest_run_is_a_json_report(monkeypatch, capsys):
    FakeCluster().install(monkeypatch)
    result = investigate("Why is not-ready-app unhealthy?")
    capsys.readouterr()

    assert main(["trace"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert (report["run_id"], report["trace_id"]) == (result["run_id"], result["trace_id"])
    assert report["request"]["question"] == "Why is not-ready-app unhealthy?"
    assert report["summary"]["result"] == "conclusion"
    assert "get_setup" in report["summary"]["tools_used"]
    types = [step["type"] for step in report["steps"]]
    assert types[0] == "model" and "tool" in types and report["steps"][-1]["span"] == "validate_answer"
    setup = next(step for step in report["steps"] if step.get("tool") == "get_setup")
    assert setup["result"]["containers"][0]["readiness_port"] == 8081
    asked = next(step for step in report["steps"] if (step.get("decision") or {}).get("call_tool") == "get_setup")
    assert asked["decision"]["arguments"]["workload"] == "not-ready-app"
    assert report["steps"][-1]["outcome"] == "accepted"
    assert report["response"]["proposed_fix"]["to_port"] == 8080


def test_table_reads_as_steps_and_an_answer(monkeypatch, capsys):
    FakeCluster().install(monkeypatch)
    result = investigate("Why is not-ready-app unhealthy?")
    capsys.readouterr()

    assert main(["trace", result["run_id"], "--table"]) == 0
    table = capsys.readouterr().out

    assert "Question  Why is not-ready-app unhealthy?" in table
    assert "decides to call get_setup" in table
    assert "listens on [8080], health check on 8081" in table
    assert "answer accepted" in table
    assert "Proposed fix: port 8081 to 8080 on not-ready-app/not-ready-app" in table


def test_list_shows_recent_runs(monkeypatch, capsys):
    FakeCluster().install(monkeypatch)
    result = investigate("Why is crashing-app unhealthy?")
    capsys.readouterr()

    assert main(["trace", "list"]) == 0
    runs = json.loads(capsys.readouterr().out)
    assert runs[0]["run_id"] == result["run_id"] and runs[0]["result"] == "conclusion"
    assert main(["trace", "list", "--table"]) == 0
    assert result["run_id"] in capsys.readouterr().out


def test_html_saves_a_page_next_to_the_trace(monkeypatch, capsys):
    FakeCluster().install(monkeypatch)
    result = investigate("Why is not-ready-app unhealthy?")
    capsys.readouterr()

    assert main(["trace", "--html"]) == 0
    assert f"traces/{result['run_id']}.html" in capsys.readouterr().out
    page = Path(result["trace_path"]).with_suffix(".html").read_text(encoding="utf-8")
    assert "<h1>Why is not-ready-app unhealthy?</h1>" in page
    assert "execute_tool get_setup" in page and "listens on [8080], health check on 8081" in page
    assert "Health-check port 8081 to 8080 on not-ready-app/not-ready-app" in page


def test_html_shows_cluster_text_as_text(monkeypatch, capsys):
    fake = FakeCluster()
    fake.logs[("crashing-app", "crashing-app-7f8d9c")]["logs"] = 'unknown APP_MODE bogus <script>alert("x")</script><img src=https://example.com/x>'
    fake.install(monkeypatch)
    result = investigate("Why is crashing-app unhealthy?")
    capsys.readouterr()

    assert main(["trace", result["run_id"], "--html"]) == 0
    page = Path(result["trace_path"]).with_suffix(".html").read_text(encoding="utf-8")
    assert "<script" not in page and "<img" not in page
    assert "&lt;script&gt;" in page
    assert "default-src 'none'" in page


def test_html_is_one_run_at_a_time(capsys):
    assert main(["trace", "list", "--html"]) == 2
    assert "--html shows one run" in capsys.readouterr().err


@pytest.mark.parametrize("run_id", ["../../etc/passwd", "/etc/passwd", "approve-../x", "abc"])
def test_only_run_ids_are_read(run_id, capsys):
    assert main(["trace", run_id]) == 2
    assert "run id" in capsys.readouterr().err


def test_no_traces_yet_is_a_clear_message(capsys):
    assert main(["trace"]) == 2
    assert "no traces yet" in capsys.readouterr().err


def test_an_old_trace_asks_for_a_new_run(monkeypatch, capsys):
    from k8s_troubleshooter.config import trace_dir

    old = trace_dir() / f"{'e' * 32}.jsonl"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({"name": "investigate", "attributes": {}}) + "\n", encoding="utf-8")
    assert main(["trace", "e" * 32]) == 2
    assert "old trace format" in capsys.readouterr().err
