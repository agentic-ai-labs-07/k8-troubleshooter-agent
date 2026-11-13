"""The answer the model must return, and the one change it may propose."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    action: str
    fact: str


class ProposedFix(BaseModel):
    namespace: str
    deployment: str
    container: str
    from_port: int
    to_port: int
    expected_effect: str


class Findings(BaseModel):
    fault_found: bool
    cause: str
    evidence: list[Evidence] = Field(default_factory=list)
    next_action: str
    proposed_fix: ProposedFix | None = None
