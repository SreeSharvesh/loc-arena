"""The rogue loop keeps running past an empty model reply (issue #28).

A reasoning model that spends its output budget on reasoning answers with empty text. OpenRouter refuses an
empty user message, so the loop must send its last real prompt again instead of feeding the empty reply back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from loc_arena.gateway.core import GenerateRequest
from loc_arena.jobsvc import RogueWorker


@dataclass
class _Reply:
    text: str


@dataclass
class _ScriptedEdge:
    """Answers each generate call with the next scripted text and keeps the prompts it was sent."""

    replies: list[str]
    prompts: list[str] = field(default_factory=list)

    def generate(self, req: GenerateRequest) -> _Reply:
        assert req.prompt is not None
        self.prompts.append(req.prompt)
        return _Reply(self.replies[len(self.prompts) - 1])


class _Identity:
    def issue(self, account: str, *, sanctioned: bool, exp: float) -> tuple[str, str]:
        return "token", "instance-1"


def _worker(edge: _ScriptedEdge) -> RogueWorker:
    return RogueWorker(
        cast(Any, edge),
        cast(Any, _Identity()),
        cast(Any, object()),
        lambda _ts: None,
        account="svc-distill",
        job_id="job-1",
        caller_identity="agent-main/distill-agent",
    )


def test_an_empty_reply_sends_the_last_prompt_again() -> None:
    edge = _ScriptedEdge(replies=["first", "", "  ", "second", "third"])

    result = _worker(edge).run_loop(start_ts=100.0, dt=10.0, n=5, seed_prompt="seed")

    assert result.calls == 5
    assert edge.prompts == ["seed", "first", "first", "first", "second"]


def test_no_prompt_the_loop_sends_is_empty() -> None:
    edge = _ScriptedEdge(replies=["", "", "", ""])

    _worker(edge).run_loop(start_ts=100.0, dt=10.0, n=4, seed_prompt="seed")

    assert all(prompt.strip() for prompt in edge.prompts)
