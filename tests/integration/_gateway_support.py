"""Test doubles for the gateway: deterministic stub providers (the only egress, replaced).

No docker and no network: the stubs replace the only egress.
"""

from __future__ import annotations

import itertools

from loc_arena.gateway.core import Message, ProviderResult, ToolSpec


def joined_content(messages: list[Message]) -> str:
    """The model input as one string; for a ``prompt`` request this is exactly the (injected) prompt."""
    return "\n\n".join(str(m["content"]) for m in messages)


class StubProvider:
    """A deterministic provider recording every model input and returning a unique completion per call.

    Unique outputs let a worker feed a call's output back as the next call's prompt to build a real
    self-feeding chain (whose fingerprints link), while distinct completions keep fingerprints distinct.
    """

    def __init__(self, *, prompt_tokens: int = 10, completion_tokens: int = 10) -> None:
        self.inputs: list[str] = []
        self._pt = prompt_tokens
        self._ct = completion_tokens
        self._n = 0

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        self.inputs.append(joined_content(messages))
        self._n += 1
        return ProviderResult(text=f"out#{self._n}", prompt_tokens=self._pt, completion_tokens=self._ct)


class JsonVerdictProvider:
    """A deterministic provider returning a fixed JSON monitor verdict (live-monitor tests, no network)."""

    def __init__(self, suspicion: float = 0.9, reason: str = "stub verdict") -> None:
        self._text = f'{{"suspicion": {suspicion}, "reason": "{reason}"}}'

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        return ProviderResult(text=self._text, prompt_tokens=1, completion_tokens=1)


class MalformedProvider:
    """A provider that returns an unparseable completion (to exercise the monitor parse-error fallback)."""

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        return ProviderResult(
            text="I think this looks fine, no JSON here.",
            prompt_tokens=1,
            completion_tokens=1,
        )


class AlternatingVerdictProvider:
    """A provider whose replies alternate: unparseable first, then a JSON verdict, and so on."""

    def __init__(self) -> None:
        self._providers = itertools.cycle((MalformedProvider(), JsonVerdictProvider()))

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        return next(self._providers).generate(model, messages, temperature, max_tokens, tools)
