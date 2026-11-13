"""The agent calls tools more than once, then names the known cause."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from k8s_troubleshooter.agent.graph import investigate
from k8s_troubleshooter.cli import main
from k8s_troubleshooter.config import max_steps
from k8s_troubleshooter.evals.graders import grade
from k8s_troubleshooter.evals.scenarios import SCENARIOS
from k8s_troubleshooter.llm.rule_based import _named_result, next_message
from k8s_troubleshooter.observability.trace_file import read_spans
from tests.fake_cluster import FakeCluster

CLIENT = "k8s_troubleshooter.clients.kubernetes"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario["id"] for scenario in SCENARIOS])
def test_known_failure(scenario, monkeypatch):
    FakeCluster().install(monkeypatch)
    result = investigate(scenario["question"])
    assert grade(result, scenario, max_steps=max_steps()) == []
    spans = read_spans(result["trace_path"])
    assert spans[0]["name"] == "invoke_agent k8s-troubleshooter"
    assert spans[0]["attributes"]["k8s_troubleshooter.question"] == scenario["question"]
    assert spans[-1]["name"] == "validate_answer"
    assert all(span["trace_id"] == result["trace_id"] for span in spans)


def test_a_loop_stops_at_the_step_limit(monkeypatch):
    fake = FakeCluster()
    fake.install(monkeypatch)
    result = investigate("Why is crashing-app unhealthy?", model=_RepeatingModel(), step_limit=2)
    assert result["stopped_reason"] == "iteration_limit"
    assert result["steps"] == 2
    assert fake.patched is None


def test_only_the_first_tool_call_runs(monkeypatch):
    fake = FakeCluster()
    seen = []

    def list_pods(namespace=""):
        seen.append("get_pod_status")
        return fake.list_pods(namespace)

    def list_events(namespace, name):
        seen.append("get_events")
        return fake.list_events(namespace, name)

    monkeypatch.setattr(f"{CLIENT}.list_pods", list_pods)
    monkeypatch.setattr(f"{CLIENT}.list_events", list_events)
    monkeypatch.setattr(f"{CLIENT}.read_logs", fake.read_logs)
    monkeypatch.setattr(f"{CLIENT}.read_setup", fake.read_setup)
    result = investigate("Why is crashing-app unhealthy?", model=_TwoCallsThenAnswer())
    assert seen == ["get_pod_status"]
    assert result["stopped_reason"] == "conclusion"


def test_a_guessed_namespace_still_searches_every_namespace(monkeypatch):
    from k8s_troubleshooter.tools.kubernetes import get_pod_status

    fake = FakeCluster()
    asked = []

    def list_pods(namespace=""):
        asked.append(namespace)
        return fake.list_pods(namespace)

    monkeypatch.setattr(f"{CLIENT}.list_pods", list_pods)
    pods = json.loads(get_pod_status.invoke({"namespace": "crashing-app"}))["pods"]
    assert asked == [""]
    assert any(pod["namespace"] == "crashing-app" and pod["app"] == "crashing-app" for pod in pods)


def test_a_shorter_name_does_not_reuse_another_setup():
    message = ToolMessage(
        content=json.dumps({"workload": "healthy-app", "found": True}),
        name="get_setup",
        tool_call_id="1",
    )
    assert _named_result([message], "get_setup", "crashing-app") is None


def test_an_unbound_tool_is_not_called():
    pods = FakeCluster().list_pods()
    messages = [
        HumanMessage(content="Why is crashing-app unhealthy?"),
        ToolMessage(content=json.dumps(pods), name="get_pod_status", tool_call_id="1"),
    ]
    answer = next_message(messages, allowed={"get_pod_status"})
    assert not answer.tool_calls
    assert "not available" in answer.content


def test_a_failed_read_is_not_a_clean_bill(monkeypatch):
    def list_pods(namespace=""):
        from k8s_troubleshooter.exceptions import CommandError

        raise CommandError("kind kubeconfig not found: missing")

    monkeypatch.setattr(f"{CLIENT}.list_pods", list_pods)
    result = investigate("Why is crashing-app unhealthy?")
    assert result["findings"]["fault_found"] is False
    assert result["findings"]["cause"].startswith("Could not read")
    assert "No fault found" not in result["findings"]["cause"]


def test_an_answer_before_a_tool_call_is_rejected():
    result = investigate("Why is crashing-app unhealthy?", model=_ImmediateAnswer())
    assert result["stopped_reason"] == "invalid_answer"
    assert result["findings"] is None
    assert "no tool ran" in result["detail"]


def test_empty_question_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        investigate("   ")


def test_a_long_question_is_rejected_before_the_model(capsys):
    assert main(["investigate", "x" * 501]) == 2
    assert "longer than 500" in capsys.readouterr().err


def test_cli_prints_the_answer(monkeypatch, capsys):
    FakeCluster().install(monkeypatch)
    assert main(["investigate", "Why is missing-config-app unhealthy?"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["proposal_id"] is None
    assert "app-settings" in payload["findings"]["cause"]


def test_each_step_is_logged_while_stdout_stays_json(monkeypatch, capsys, caplog):
    FakeCluster().install(monkeypatch)
    assert main(["investigate", "Why is crashing-app unhealthy?"]) == 0
    json.loads(capsys.readouterr().out)
    events = [(record.getMessage(), record.event_fields) for record in caplog.records if hasattr(record, "event_fields")]
    names = [name for name, _fields in events]
    assert names[0] == "run_started" and events[0][1]["question"] == "Why is crashing-app unhealthy?"
    tool_calls = [fields for name, fields in events if name == "tool_call"]
    assert any(fields["tool"] == "get_pod_logs" and fields["status"] == "ok" for fields in tool_calls)
    finished = dict(events)["run_finished"]
    assert finished["stopped_reason"] == "conclusion"
    assert "get_pod_logs" in finished["tools_used"] and finished["tool_calls"] >= 2


def test_a_model_failure_is_a_clean_answer(monkeypatch, capsys):
    FakeCluster().install(monkeypatch)
    monkeypatch.setattr("k8s_troubleshooter.cli.investigate", lambda question: investigate(question, model=_FailingModel()))
    assert main(["investigate", "Why is crashing-app unhealthy?"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped_reason"] == "model_error"
    assert "CERTIFICATE_VERIFY_FAILED" in payload["detail"]
    assert "CERTIFICATE_VERIFY_FAILED" in Path(payload["trace_path"]).read_text(encoding="utf-8")


def test_eval_grades_the_three_scenarios(monkeypatch, capsys):
    fake = FakeCluster()
    fake.install(monkeypatch)
    monkeypatch.setattr("k8s_troubleshooter.cli.list_pods", fake.list_pods)
    assert main(["eval"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert (report["passed"], report["total"]) == (3, 3)


def test_eval_names_a_scenario_that_is_not_applied(monkeypatch, capsys):
    fake = FakeCluster()
    fake.pods = [item for item in fake.pods if item["namespace"] != "missing-config-app"]
    fake.install(monkeypatch)
    monkeypatch.setattr("k8s_troubleshooter.cli.list_pods", fake.list_pods)
    assert main(["eval"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] == 2
    missing = next(item for item in report["scenarios"] if item["scenario"] == "missing-config-app")
    assert "scenario apply missing-config-app" in missing["problems"][0]


class _FailingModel(BaseChatModel):
    """Fails like the SDK does behind a TLS-intercepting proxy."""

    @property
    def _llm_type(self) -> str:
        return "failing"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        from langchain_core.exceptions import ModelError

        raise ModelError("Connection error.") from OSError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate")


class _ImmediateAnswer(BaseChatModel):
    """Answers on the first turn, before any tool call."""

    @property
    def _llm_type(self) -> str:
        return "immediate"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = AIMessage(
            content=json.dumps(
                {
                    "fault_found": True,
                    "cause": "guessed",
                    "evidence": [{"action": "get_pod_status", "fact": "unseen"}],
                    "next_action": "No change.",
                    "proposed_fix": None,
                }
            )
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class _TwoCallsThenAnswer(BaseChatModel):
    """Asks for two tools at once, then answers. Only the first call may run."""

    turns: int = 0

    @property
    def _llm_type(self) -> str:
        return "two-calls"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.turns += 1
        if self.turns > 1:
            message = AIMessage(
                content=json.dumps(
                    {
                        "fault_found": True,
                        "cause": "stopped after one tool call",
                        "evidence": [{"action": "get_pod_status", "fact": "seen"}],
                        "next_action": "No change.",
                        "proposed_fix": None,
                    }
                )
            )
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_pod_status", "args": {}, "id": "first", "type": "tool_call"},
                    {"name": "get_events", "args": {"namespace": "crashing-app", "name": "crashing-app-7f8d9c"}, "id": "second", "type": "tool_call"},
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


class _RepeatingModel(BaseChatModel):
    """Always asks for the same tool, so the step limit has to stop the run."""

    steps_sent: int = 0

    @property
    def _llm_type(self) -> str:
        return "repeating"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.steps_sent += 1
        message = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "get_pod_status",
                    "args": {},
                    "id": f"call-{self.steps_sent}",
                    "type": "tool_call",
                }
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])
