"""Picks the model. Another provider is one more branch here returning a LangChain chat model."""

from __future__ import annotations

import os

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from k8s_troubleshooter.config import max_tokens, model_mode, openai_api_key
from k8s_troubleshooter.exceptions import SafetyError
from k8s_troubleshooter.llm.rule_based import RuleBasedModel

DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"


def build_model() -> BaseChatModel:
    mode = model_mode()
    if mode == "rules":
        return RuleBasedModel()
    if mode != "live":
        raise SafetyError("AGENT_MODEL must be live or rules")
    key = openai_api_key()
    if not key:
        raise SafetyError(
            "an OpenAI key is required when AGENT_MODEL=live: set OPENAI_API_KEY, or set AGENT_MODEL=rules to run without one"
        )
    return ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL,
        api_key=key,
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
        max_tokens=max_tokens(),
        temperature=0,
    )


def describe_model(model: BaseChatModel) -> tuple[str, str]:
    """(model name, provider name) for traces and logs, such as ("gpt-4.1-mini", "openai")."""
    kind = getattr(model, "_llm_type", "unknown")
    if isinstance(model, ChatOpenAI):
        return model.model_name, "openai"
    return kind, kind
