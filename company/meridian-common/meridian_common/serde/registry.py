"""A versioned schema registry for message payloads.

Every message type registers one or more versioned schemas by ``(name, version)``. Producers encode against
the
latest registered version; consumers validate an incoming payload against the version stamped on its envelope.
The registry is the single place that knows which schema versions exist, so a rolling upgrade can add a new
version without breaking older consumers.
"""

from __future__ import annotations

from typing import Any

from meridian_common.config.schema import Schema
from meridian_common.errors import NotFoundError, ValidationError


class SchemaRegistry:
    """Holds versioned :class:`~meridian_common.config.schema.Schema` objects keyed by ``(name, version)``."""

    def __init__(self) -> None:
        """Start with an empty registry."""
        self._schemas: dict[tuple[str, int], Schema] = {}
        self._latest: dict[str, int] = {}

    def register(self, name: str, version: int, schema: Schema) -> None:
        """Register ``schema`` for ``(name, version)``; a duplicate version is a ``ValidationError``."""
        key = (name, version)
        if key in self._schemas:
            raise ValidationError(f"schema {name!r} v{version} already registered", path=name)
        self._schemas[key] = schema
        self._latest[name] = max(self._latest.get(name, 0), version)

    def latest_version(self, name: str) -> int:
        """The highest registered version of ``name`` (raises ``NotFoundError`` if unknown)."""
        if name not in self._latest:
            raise NotFoundError(f"no schema registered for {name!r}", code="not_found.schema")
        return self._latest[name]

    def get(self, name: str, version: int) -> Schema:
        """The schema for ``(name, version)`` (raises ``NotFoundError`` if absent)."""
        key = (name, version)
        if key not in self._schemas:
            raise NotFoundError(f"no schema {name!r} v{version}", code="not_found.schema")
        return self._schemas[key]

    def validate(self, name: str, version: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate ``payload`` against the registered ``(name, version)`` schema."""
        return self.get(name, version).validate(payload)

    def names(self) -> list[str]:
        """The registered message names, sorted."""
        return sorted(self._latest)
