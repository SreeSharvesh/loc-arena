"""Scorer and verifier registries (dispatch by name).

Enforces config over code: ``config.main_task.scorer`` and
``config.side_task.verifier`` are dispatched BY NAME through these registries, not hardcoded. Adding a
(main task, side task) pair at a new point becomes: register a scorer and a verifier by name and reference
them from config. A verifier's landing-predicate STRUCTURE stays the identity of that verifier version;
only the numeric thresholds are config.

This module holds only the registries and decorators (no heavy imports), so config, scenarios, and
verifiers can import it without cycles. Callables are generic so a scorer/verifier takes what it needs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

_Scorer = Callable[..., Any]
_Verifier = Callable[..., Any]

SCORER_REGISTRY: dict[str, _Scorer] = {}
VERIFIER_REGISTRY: dict[str, _Verifier] = {}

S = TypeVar("S", bound=_Scorer)
V = TypeVar("V", bound=_Verifier)


class RegistryError(KeyError):
    """A scorer/verifier name was requested that is not registered."""


def register_scorer(name: str) -> Callable[[S], S]:
    """Register a main-task scorer under ``name`` (used as a decorator)."""

    def deco(fn: S) -> S:
        if name in SCORER_REGISTRY and SCORER_REGISTRY[name] is not fn:
            raise ValueError(f"scorer {name!r} is already registered to a different callable")
        SCORER_REGISTRY[name] = fn
        return fn

    return deco


def register_verifier(name: str) -> Callable[[V], V]:
    """Register a side-task verifier under ``name`` (used as a decorator)."""

    def deco(fn: V) -> V:
        if name in VERIFIER_REGISTRY and VERIFIER_REGISTRY[name] is not fn:
            raise ValueError(f"verifier {name!r} is already registered to a different callable")
        VERIFIER_REGISTRY[name] = fn
        return fn

    return deco


def get_scorer(name: str) -> _Scorer:
    """Return the registered scorer, or raise ``RegistryError`` with the known names."""
    if name not in SCORER_REGISTRY:
        raise RegistryError(f"unknown scorer {name!r}; registered: {sorted(SCORER_REGISTRY)}")
    return SCORER_REGISTRY[name]


def get_verifier(name: str) -> _Verifier:
    """Return the registered verifier, or raise ``RegistryError`` with the known names."""
    if name not in VERIFIER_REGISTRY:
        raise RegistryError(f"unknown verifier {name!r}; registered: {sorted(VERIFIER_REGISTRY)}")
    return VERIFIER_REGISTRY[name]


def is_scorer(name: str) -> bool:
    """Whether a scorer is registered under ``name``."""
    return name in SCORER_REGISTRY


def is_verifier(name: str) -> bool:
    """Whether a verifier is registered under ``name``."""
    return name in VERIFIER_REGISTRY
