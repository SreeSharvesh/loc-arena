"""Constrained decoding: restrict the next token to a grammar or an allowed set.

Guided generation forces the output to obey a structure (a JSON shape, an enum, a regex-like FSM). A
:class:`TokenConstraint` returns the set of tokens allowed to follow the current output; the sampler
intersects
its candidate set with that allowed set before sampling. The building block is a small deterministic finite
automaton (:class:`DFAConstraint`) plus an :class:`AllowedSetConstraint` for fixed vocabularies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from meridian_serving.errors import SamplerError


class TokenConstraint(Protocol):
    """Given the tokens emitted so far, return the set of tokens allowed next (empty means 'stop')."""

    def allowed(self, emitted: tuple[int, ...]) -> set[int]:
        """The set of tokens permitted to follow ``emitted``."""
        ...

    def is_terminal(self, emitted: tuple[int, ...]) -> bool:
        """Whether ``emitted`` is a complete, valid sequence."""
        ...


class AllowedSetConstraint:
    """Restricts every position to a fixed allowed token set (an enum-style constraint)."""

    def __init__(self, allowed: set[int], *, min_len: int = 1, max_len: int | None = None) -> None:
        """Hold the allowed set and the length bounds."""
        if not allowed:
            raise SamplerError("allowed set must be non-empty")
        self._allowed = set(allowed)
        self._min = min_len
        self._max = max_len

    def allowed(self, emitted: tuple[int, ...]) -> set[int]:
        """The allowed set, or empty once the max length is reached."""
        if self._max is not None and len(emitted) >= self._max:
            return set()
        return set(self._allowed)

    def is_terminal(self, emitted: tuple[int, ...]) -> bool:
        """Whether ``emitted`` is at least ``min_len`` tokens long."""
        return len(emitted) >= self._min


@dataclass
class DFATransition:
    """A DFA state's transitions: token -> next state, plus whether the state is accepting."""

    accepting: bool = False
    edges: dict[int, int] = field(default_factory=dict)


class DFAConstraint:
    """A deterministic finite automaton over token ids; allowed tokens are the current state's out-edges."""

    def __init__(self, states: list[DFATransition], *, start: int = 0) -> None:
        """Hold the DFA states and the start state."""
        if not states:
            raise SamplerError("a DFA needs at least one state")
        self._states = states
        self._start = start

    def _run(self, emitted: tuple[int, ...]) -> int | None:
        state = self._start
        for token in emitted:
            edges = self._states[state].edges
            if token not in edges:
                return None  # rejected: no valid transition
            state = edges[token]
        return state

    def allowed(self, emitted: tuple[int, ...]) -> set[int]:
        """The out-edge tokens of the DFA state reached by ``emitted`` (empty if rejected)."""
        state = self._run(emitted)
        if state is None:
            return set()
        return set(self._states[state].edges)

    def is_terminal(self, emitted: tuple[int, ...]) -> bool:
        """Whether ``emitted`` leaves the DFA in an accepting state."""
        state = self._run(emitted)
        return state is not None and self._states[state].accepting


class ConstrainedDecoder:
    """Applies a :class:`TokenConstraint` to greedily pick the highest-scoring allowed token each step."""

    def __init__(self, constraint: TokenConstraint) -> None:
        """Hold the token constraint."""
        self._constraint = constraint

    def decode(self, score_fn: object, *, max_tokens: int) -> tuple[int, ...]:  # pragma: no cover - see step
        """Not used directly; :meth:`step` is the unit of constrained decoding."""
        raise NotImplementedError

    def step(self, emitted: tuple[int, ...], logits: list[float]) -> int | None:
        """Pick the highest-logit token allowed after ``emitted``; ``None`` if the sequence must stop."""
        allowed = self._constraint.allowed(emitted)
        if not allowed:
            return None
        best = max(allowed, key=lambda t: (logits[t] if 0 <= t < len(logits) else float("-inf"), -t))
        return best
