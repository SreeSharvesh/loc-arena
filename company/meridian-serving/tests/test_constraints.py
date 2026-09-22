from __future__ import annotations

import pytest

from meridian_serving.api import parse_request, render_response
from meridian_serving.errors import SamplerError
from meridian_serving.sampler import (
    AllowedSetConstraint,
    ConstrainedDecoder,
    DFAConstraint,
    DFATransition,
)
from meridian_serving.types import Priority, Response


def test_allowed_set_constraint_restricts_tokens() -> None:
    c = AllowedSetConstraint({1, 2, 3}, min_len=1, max_len=2)
    assert c.allowed(()) == {1, 2, 3}
    assert c.allowed((1, 2)) == set()  # max length reached
    assert c.is_terminal((1,)) and not c.is_terminal(())


def test_allowed_set_requires_non_empty() -> None:
    with pytest.raises(SamplerError):
        AllowedSetConstraint(set())


def test_dfa_constraint_follows_edges() -> None:
    # state 0 -(1)-> 1 -(2)-> 2(accept)
    dfa = DFAConstraint(
        [
            DFATransition(edges={1: 1}),
            DFATransition(edges={2: 2}),
            DFATransition(accepting=True),
        ]
    )
    assert dfa.allowed(()) == {1}
    assert dfa.allowed((1,)) == {2}
    assert dfa.allowed((1, 2)) == set() and dfa.is_terminal((1, 2))
    assert dfa.allowed((9,)) == set()  # invalid transition -> rejected


def test_constrained_decoder_picks_allowed_best() -> None:
    dec = ConstrainedDecoder(AllowedSetConstraint({1, 2}, max_len=3))
    logits = [9.0, 0.0, 5.0, 9.0]  # token 3 has top logit but is not allowed
    assert dec.step((), logits) == 2  # best among {1,2}
    assert dec.step((1, 2, 3), logits) is None  # past max length -> stop


def test_openai_parse_and_render_roundtrip() -> None:
    parsed = parse_request(
        {"id": "req-1", "prompt": [1, 2, 3], "max_tokens": 4, "temperature": 0.7, "priority": "high"}
    )
    assert parsed.request.request_id == "req-1" and parsed.request.priority is Priority.HIGH
    assert parsed.request.max_tokens == 4

    wire = render_response(Response(request_id="req-1", tokens=(5, 6), prompt_len=3, finish_reason="length"))
    assert wire["id"] == "req-1"
    assert wire["choices"][0]["tokens"] == [5, 6]
    assert wire["usage"]["total_tokens"] == 5


def test_openai_parse_rejects_bad_payload() -> None:
    from meridian_common.errors import ValidationError

    with pytest.raises(ValidationError):
        parse_request({"id": "x", "prompt": [1], "temperature": -1.0})  # temperature must be > 0
