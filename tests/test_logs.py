"""Every command's log lines are kept in one file, and the logs command and the trace both show them."""

from __future__ import annotations

import json

from k8s_troubleshooter.cli import main
from k8s_troubleshooter.config import log_file
from k8s_troubleshooter.exceptions import CommandError
from k8s_troubleshooter.observability import logs
from k8s_troubleshooter.observability.log_file import read_entries
from tests.fake_cluster import FakeCluster

CLIENT = "k8s_troubleshooter.clients.kubernetes"


def _investigate(monkeypatch, capsys, question="Why is not-ready-app unhealthy?") -> dict:
    FakeCluster().install(monkeypatch)
    assert main(["investigate", question]) == 0
    return json.loads(capsys.readouterr().out)


def test_a_run_is_saved_as_json_lines(monkeypatch, capsys):
    answer = _investigate(monkeypatch, capsys)
    entries = read_entries()
    names = [entry["event"] for entry in entries]

    assert names[0] == "run_started" and "run_finished" in names and names[-1] == "fix_proposed"
    assert all(entry["run_id"] == answer["run_id"] for entry in entries)
    assert all(entry["service"] == "local" and entry["command"] == "investigate" for entry in entries)
    tools = [entry for entry in entries if entry["event"] == "tool_call"]
    assert tools and all(entry["trace_id"] == answer["trace_id"] for entry in tools)


def test_the_file_is_json_even_when_the_screen_is_text(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "text")
    _investigate(monkeypatch, capsys)
    lines = log_file().read_text(encoding="utf-8").splitlines()
    assert lines and all(json.loads(line)["event"] for line in lines)


def test_logs_shows_every_run_oldest_first(monkeypatch, capsys):
    first = _investigate(monkeypatch, capsys, "Why is crashing-app unhealthy?")
    second = _investigate(monkeypatch, capsys)

    assert main(["logs"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[3:5] == ["local", "run_started"]
    assert out.index(next(line for line in out if second["run_id"] in line)) > out.index(next(line for line in out if first["run_id"] in line))


def test_logs_filters(monkeypatch, capsys):
    first = _investigate(monkeypatch, capsys, "Why is crashing-app unhealthy?")
    _investigate(monkeypatch, capsys)

    assert main(["logs", "--run", first["run_id"], "--json"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines and {line["run_id"] for line in lines} == {first["run_id"]}

    assert main(["logs", "--event", "run_finished", "--limit", "1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1 and "run_finished" in out[0]

    assert main(["logs", "--level", "warning"]) == 0
    assert capsys.readouterr().out.strip() == "No log lines match."

    assert main(["logs", "--since", "1h", "--service", "local", "--event", "run_started"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_logs_rejects_a_bad_filter(capsys):
    assert main(["logs", "--since", "yesterday"]) == 2
    assert "30s, 10m, 2h, or 1d" in capsys.readouterr().err
    assert main(["logs", "--level", "loud"]) == 2


def test_a_failure_is_kept_with_its_reason(monkeypatch, capsys):
    def list_pods(namespace=""):
        raise CommandError("error: You must be logged in to the server")

    monkeypatch.setattr(f"{CLIENT}.list_pods", list_pods)
    main(["investigate", "Why is crashing-app unhealthy?"])
    capsys.readouterr()

    assert main(["logs", "--level", "warning"]) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "tool_call" in out and "must be logged in" in out


def test_secrets_never_reach_the_file(monkeypatch, capsys):
    token = "sk-" + "y" * 24

    def list_pods(namespace=""):
        raise CommandError(f"request failed: password=hunter2 token {token}")

    monkeypatch.setattr(f"{CLIENT}.list_pods", list_pods)
    main(["investigate", "Why is crashing-app unhealthy?"])
    saved = log_file().read_text(encoding="utf-8")
    assert "hunter2" not in saved and token not in saved


def test_old_lines_rotate_and_are_still_read(monkeypatch, capsys):
    monkeypatch.setattr(logs, "MAX_FILE_BYTES", 5000)
    for _ in range(3):
        _investigate(monkeypatch, capsys)
    assert log_file().with_name(log_file().name + ".1").is_file()
    assert sum(1 for entry in read_entries() if entry["event"] == "run_started") == 3


def test_an_unwritable_log_file_does_not_stop_the_agent(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("LOG_FILE", str(tmp_path))
    FakeCluster().install(monkeypatch)
    assert main(["investigate", "Why is crashing-app unhealthy?"]) == 0
    assert "log_file_unavailable" in capsys.readouterr().err


def test_the_trace_shows_its_own_log_lines(monkeypatch, capsys):
    first = _investigate(monkeypatch, capsys)
    _investigate(monkeypatch, capsys, "Why is crashing-app unhealthy?")

    assert main(["trace", first["run_id"]]) == 0
    report = json.loads(capsys.readouterr().out)
    events = [line["event"] for line in report["logs"]]
    assert events[0] == "run_started" and "fix_proposed" in events
    setup_step = next(step["step"] for step in report["steps"] if step.get("tool") == "get_setup")
    setup_log = next(line for line in report["logs"] if line["event"] == "tool_call" and line.get("tool") == "get_setup")
    assert setup_log["step"] == setup_step

    assert main(["trace", first["run_id"], "--table"]) == 0
    assert f"logs --run {first['run_id']}" in capsys.readouterr().out
    assert main(["trace", first["run_id"], "--html"]) == 0
    capsys.readouterr()
    page = log_file().parent.parent.joinpath(f"{first['run_id']}.html").read_text(encoding="utf-8")
    assert "<h2>Logs</h2>" in page and f'href="#step-{setup_step}"' in page
