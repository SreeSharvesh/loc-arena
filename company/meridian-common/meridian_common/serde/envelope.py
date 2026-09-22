"""Versioned message envelopes.

Every message on the event bus or a client wire is wrapped in an :class:`Envelope`: the payload plus metadata
(message name, schema version, a content id fingerprint, a correlation id, and a timestamp). The envelope is
what gets serialized; a consumer validates the payload against the registry version stamped on the
envelope, so
producers and consumers can evolve independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from meridian_common.errors import ValidationError
from meridian_common.serde.canonical import canonical_json, fingerprint
from meridian_common.serde.registry import SchemaRegistry


@dataclass(frozen=True)
class Envelope:
    """A versioned message: ``name``/``version`` name the schema, ``content_id`` fingerprints the payload."""

    name: str
    version: int
    payload: dict[str, Any]
    content_id: str
    correlation_id: str | None = None
    ts: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def wrap(
        name: str,
        version: int,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
        ts: float = 0.0,
        headers: dict[str, str] | None = None,
    ) -> Envelope:
        """Wrap ``payload`` with a computed content-id fingerprint and metadata."""
        return Envelope(
            name=name,
            version=version,
            payload=dict(payload),
            content_id=fingerprint({"name": name, "version": version, "payload": payload}),
            correlation_id=correlation_id,
            ts=ts,
            headers=dict(headers or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the envelope to a JSON-friendly mapping."""
        return {
            "name": self.name,
            "version": self.version,
            "payload": self.payload,
            "content_id": self.content_id,
            "correlation_id": self.correlation_id,
            "ts": self.ts,
            "headers": dict(self.headers),
        }

    def to_json(self) -> str:
        """The canonical JSON string for the envelope."""
        return canonical_json(self.to_dict())

    @staticmethod
    def from_dict(data: dict[str, Any]) -> Envelope:
        """Reconstruct an envelope from a mapping, verifying the content id."""
        env = Envelope(
            name=str(data["name"]),
            version=int(data["version"]),
            payload=dict(data["payload"]),
            content_id=str(data["content_id"]),
            correlation_id=data.get("correlation_id"),
            ts=float(data.get("ts", 0.0)),
            headers=dict(data.get("headers", {})),
        )
        expected = fingerprint({"name": env.name, "version": env.version, "payload": env.payload})
        if expected != env.content_id:
            raise ValidationError("envelope content_id does not match payload", path=env.name)
        return env

    def validate_against(self, registry: SchemaRegistry) -> dict[str, Any]:
        """Validate the payload against the registry's ``(name, version)`` schema; return it normalized."""
        return registry.validate(self.name, self.version, self.payload)
