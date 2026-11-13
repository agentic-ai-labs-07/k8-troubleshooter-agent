"""What the loop carries from one step to the next."""

from __future__ import annotations

from typing import Annotated

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    steps: int
    findings: dict | None
    stopped_reason: str
