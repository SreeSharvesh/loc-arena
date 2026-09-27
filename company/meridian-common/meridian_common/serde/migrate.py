"""Schema migration chains: upgrade a payload from an old version to the latest.

A rolling upgrade adds a new schema version; a :class:`Migrator` holds per-message ``(from_version ->
to_version)`` transform functions and applies them in order to bring an old payload up to a target
version. This
lets a consumer accept an older producer's message and normalize it forward before validating.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from meridian_common.errors import NotFoundError

Migration = Callable[[dict[str, Any]], dict[str, Any]]


class Migrator:
    """Holds forward migrations per message name and applies a chain from a source to a target version."""

    def __init__(self) -> None:
        """Start with no migrations."""
        self._steps: dict[str, dict[int, Migration]] = {}

    def register(self, name: str, from_version: int, migration: Migration) -> None:
        """Register a step migrating ``name`` from ``from_version`` to ``from_version + 1``."""
        self._steps.setdefault(name, {})[from_version] = migration

    def can_migrate(self, name: str, from_version: int, to_version: int) -> bool:
        """Whether a full chain of steps exists from ``from_version`` to ``to_version``."""
        steps = self._steps.get(name, {})
        return all(v in steps for v in range(from_version, to_version))

    def migrate(
        self,
        name: str,
        payload: dict[str, Any],
        from_version: int,
        to_version: int,
    ) -> dict[str, Any]:
        """Apply the chain of forward migrations to bring ``payload`` up to ``to_version``.

        Downgrades are not supported (``to_version < from_version`` is an error); a missing step raises
        :class:`NotFoundError` naming the gap.
        """
        if to_version < from_version:
            raise ValueError("cannot migrate to an older version")
        steps = self._steps.get(name, {})
        current = dict(payload)
        for version in range(from_version, to_version):
            if version not in steps:
                raise NotFoundError(
                    f"no migration for {name!r} v{version}->v{version + 1}",
                    code="not_found.migration",
                )
            current = steps[version](current)
        return current
