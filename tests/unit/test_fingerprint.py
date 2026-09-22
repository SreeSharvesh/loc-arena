from __future__ import annotations

from loc_arena.logging_.events import canonicalize, fingerprint


def test_canonicalize_is_order_independent() -> None:
    assert canonicalize({"a": 1, "b": 2}) == canonicalize({"b": 2, "a": 1})
    assert canonicalize({"x": {"p": 1, "q": 2}}) == canonicalize({"x": {"q": 2, "p": 1}})


def test_canonicalize_has_no_incidental_whitespace() -> None:
    assert canonicalize({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert b" " not in canonicalize({"a": "v", "b": [1, 2, 3]})


def test_fingerprint_stable_and_order_independent() -> None:
    payload = {"caller": "agent-main", "seq": 3, "nested": {"z": 1, "a": 2}}
    reordered = {"nested": {"a": 2, "z": 1}, "seq": 3, "caller": "agent-main"}
    assert fingerprint(payload) == fingerprint(reordered)
    # Stable across repeated calls (a fixed known digest length + determinism).
    assert fingerprint(payload) == fingerprint(payload)
    assert len(fingerprint(payload)) == 64


def test_fingerprint_differs_for_different_payloads() -> None:
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})
    assert fingerprint({"a": 1, "b": 2}) != fingerprint({"a": 1})
    # Distinct types must not collide.
    assert fingerprint({"a": 1}) != fingerprint({"a": "1"})
