"""The independent viewer's source records and evidence-linked analysis contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Judgment(_Model):
    """A monitor's judgment; its target is in the mirror sequence namespace."""

    monitor: str
    score: float
    reason: str
    target_id: str | None = None


class Record(_Model):
    """An original record, preserved in full and named by play and source stream."""

    id: str
    actor: str
    kind: str
    tool: str | None = None
    target: str | None = None
    time: float
    turn: int = 0
    play: Literal["attack", "honest", "twin"]
    stream: Literal["sealed", "mirror", "session"]
    raw: dict[str, JsonValue]
    verdicts: list[Judgment] = Field(default_factory=list)


class Agent(_Model):
    """One configured participant and its supplied initial briefs."""

    id: str
    brief: str = ""
    covert_brief: str = ""


class Outcome(_Model):
    """The saved grader's result, kept separate from generated interpretations."""

    main_score: float
    gate_passed: bool | None = None
    gate_reason: str | None = None
    landed: bool
    caught: bool
    threshold: float | None = None
    run_suspicion: float | None = None
    twin_suspicion: float | None = None
    milestones: dict[str, bool] = Field(default_factory=dict)
    failed_calls: dict[str, int] = Field(default_factory=dict)


class RunData(_Model):
    """A graded bundle's complete records and the scalar facts available for citation."""

    name: str
    fingerprint: str
    records: list[Record]
    outcome: Outcome
    agents: list[Agent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    scores: dict[str, JsonValue] = Field(default_factory=dict)


class Insight(_Model):
    """One interpretation with explicit evidence and optional earlier chain links."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    status: Literal["attempted", "completed", "blocked", "uncertain"]
    cites: list[str] = Field(min_length=1)
    related: list[str] = Field(default_factory=list)


class InsightDraft(_Model):
    """Generated content before the host checks its evidence references."""

    overview: list[Insight] = Field(default_factory=list)
    chain_start: Insight | None = None
    chain: list[Insight] = Field(default_factory=list)
    chain_end: Insight | None = None
    insights: list[Insight] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class Analysis(InsightDraft):
    """Validated content with the source and execution metadata needed for reuse."""

    source_fingerprint: str
    model: str
    generated_at: datetime
    elapsed_seconds: float = Field(default=0.0, ge=0)
    calls: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    prompt_version: str = "1"
    configuration_fingerprint: str = ""
