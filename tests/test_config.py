"""Where the OpenAI key comes from: the environment, or the Compose secret file."""

from __future__ import annotations

import pytest

from k8s_troubleshooter.config import model_mode, openai_api_key
from k8s_troubleshooter.exceptions import SafetyError
from k8s_troubleshooter.llm.provider import build_model
from k8s_troubleshooter.llm.rule_based import RuleBasedModel


def test_live_model_is_the_default(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL")
    assert model_mode() == "live"


def test_key_is_read_from_the_secret_file(monkeypatch, tmp_path):
    secret = tmp_path / "openai_api_key"
    secret.write_text("sk-test-from-file\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(secret))
    assert openai_api_key() == "sk-test-from-file"


def test_environment_key_wins_over_the_file(monkeypatch, tmp_path):
    secret = tmp_path / "openai_api_key"
    secret.write_text("sk-test-from-file", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(secret))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-from-env")
    assert openai_api_key() == "sk-test-from-env"


def test_live_model_without_a_key_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MODEL", "live")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(tmp_path / "missing"))
    with pytest.raises(SafetyError, match="OpenAI key is required"):
        build_model()


def test_rules_mode_needs_no_key():
    assert isinstance(build_model(), RuleBasedModel)


def test_an_unknown_model_mode_is_refused(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "guided")
    with pytest.raises(SafetyError, match="live or rules"):
        build_model()
