"""Feature flags: deterministic percentage rollouts and per-entity overrides.

A :class:`FeatureFlags` set is built from config. ``is_enabled(name, entity_id)`` is deterministic in the
entity id (the same id always lands in the same bucket for a given rollout), so a rollout is reproducible and
an entity does not flap between calls. Explicit per-entity allow/deny overrides win over the percentage.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from meridian_common.errors import NotFoundError


@dataclass(frozen=True)
class Flag:
    """One feature flag: a default, a rollout percentage (0..100), and per-entity overrides."""

    name: str
    default: bool = False
    rollout_percent: int = 0
    enabled_for: frozenset[str] = field(default_factory=frozenset)
    disabled_for: frozenset[str] = field(default_factory=frozenset)

    def _bucket(self, entity_id: str) -> int:
        digest = hashlib.sha256(f"{self.name}:{entity_id}".encode()).digest()
        return int.from_bytes(digest[:4], "big") % 100

    def is_enabled(self, entity_id: str | None = None) -> bool:
        """Whether the flag is on for ``entity_id`` (deterministic; overrides beat the percentage)."""
        if entity_id is not None:
            if entity_id in self.disabled_for:
                return False
            if entity_id in self.enabled_for:
                return True
        if self.rollout_percent <= 0:
            return self.default
        if self.rollout_percent >= 100:
            return True
        if entity_id is None:
            return self.default
        return self._bucket(entity_id) < self.rollout_percent


class FeatureFlags:
    """A registry of :class:`Flag` objects, typically built from a config block."""

    def __init__(self, flags: dict[str, Flag] | None = None) -> None:
        """Hold the flags by name."""
        self._flags: dict[str, Flag] = dict(flags or {})

    @staticmethod
    def from_config(raw: dict[str, Any]) -> FeatureFlags:
        """Build flags from ``{name: {default, rollout_percent, enabled_for, disabled_for}}``."""
        flags: dict[str, Flag] = {}
        for name, spec in raw.items():
            spec = spec if isinstance(spec, dict) else {"default": bool(spec)}
            flags[name] = Flag(
                name=name,
                default=bool(spec.get("default", False)),
                rollout_percent=int(spec.get("rollout_percent", 0)),
                enabled_for=frozenset(str(x) for x in spec.get("enabled_for", ())),
                disabled_for=frozenset(str(x) for x in spec.get("disabled_for", ())),
            )
        return FeatureFlags(flags)

    def is_enabled(self, name: str, entity_id: str | None = None) -> bool:
        """Whether flag ``name`` is on for ``entity_id``; unknown flags raise ``NotFoundError``."""
        if name not in self._flags:
            raise NotFoundError(f"unknown feature flag {name!r}", code="not_found.flag")
        return self._flags[name].is_enabled(entity_id)

    def names(self) -> list[str]:
        """The registered flag names, sorted."""
        return sorted(self._flags)
