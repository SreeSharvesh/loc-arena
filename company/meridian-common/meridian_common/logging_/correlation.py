"""Correlation ids: a per-context id that threads through logs and cross-service calls.

A correlation id ties every log line and downstream call of one logical request together. It lives in a
context-local stack so nested scopes inherit the current id, and :func:`new_correlation_id` derives a stable,
deterministic id from a seed when one is given (so tests and replays are reproducible) or a random one
otherwise.
"""

from __future__ import annotations

import contextvars
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

_current: contextvars.ContextVar[str | None] = contextvars.ContextVar("meridian_correlation_id", default=None)


def new_correlation_id(seed: str | None = None) -> str:
    """A fresh correlation id: deterministic from ``seed`` when given (``cid-<hex>``), else random."""
    if seed is not None:
        return "cid-" + uuid.uuid5(uuid.NAMESPACE_URL, f"meridian:{seed}").hex[:16]
    return "cid-" + os.urandom(8).hex()


def current_correlation_id() -> str | None:
    """The correlation id for the current context, or ``None`` if none is set."""
    return _current.get()


def get_or_start(seed: str | None = None) -> str:
    """Return the current correlation id, starting a new one (and setting it) if none is active."""
    existing = _current.get()
    if existing is not None:
        return existing
    cid = new_correlation_id(seed)
    _current.set(cid)
    return cid


@contextmanager
def correlation_scope(correlation_id: str | None = None, *, seed: str | None = None) -> Iterator[str]:
    """Bind a correlation id for the duration of the ``with`` block, restoring the previous id on exit."""
    cid = correlation_id or new_correlation_id(seed)
    token = _current.set(cid)
    try:
        yield cid
    finally:
        _current.reset(token)
