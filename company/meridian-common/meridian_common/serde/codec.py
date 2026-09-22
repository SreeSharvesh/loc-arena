"""A pluggable codec registry for message payloads.

The wire format is not fixed to JSON everywhere: a :class:`Codec` encodes/decodes a mapping to and from bytes,
and the :class:`CodecRegistry` picks one by content-type. The default is canonical JSON. A codec is symmetric:
``decode(encode(x)) == x`` for JSON-safe ``x``. This is where a future binary codec would slot in without
touching producers or consumers.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from meridian_common.errors import NotFoundError, ValidationError
from meridian_common.serde.canonical import canonical_bytes


class Codec(Protocol):
    """Encodes a mapping to bytes and back, identified by a stable ``content_type``."""

    @property
    def content_type(self) -> str:
        """The MIME-like content type this codec handles (e.g. ``application/json``)."""
        ...

    def encode(self, payload: dict[str, Any]) -> bytes:
        """Encode ``payload`` to bytes."""
        ...

    def decode(self, data: bytes) -> dict[str, Any]:
        """Decode ``data`` back to a mapping."""
        ...


class JsonCodec:
    """Canonical-JSON codec: deterministic, order-independent encoding."""

    content_type = "application/json"

    def encode(self, payload: dict[str, Any]) -> bytes:
        """Encode ``payload`` to canonical JSON bytes."""
        return canonical_bytes(payload)

    def decode(self, data: bytes) -> dict[str, Any]:
        """Decode canonical JSON bytes to a mapping (must be an object)."""
        obj = json.loads(data.decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValidationError("decoded payload is not a mapping", path="<root>")
        return obj


class CodecRegistry:
    """Selects a :class:`Codec` by content type; canonical JSON is registered as the default."""

    def __init__(self) -> None:
        """Register the default JSON codec."""
        self._codecs: dict[str, Codec] = {}
        self._default: str | None = None
        self.register(JsonCodec(), default=True)

    def register(self, codec: Codec, *, default: bool = False) -> None:
        """Register ``codec`` by its content type; optionally make it the default."""
        self._codecs[codec.content_type] = codec
        if default or self._default is None:
            self._default = codec.content_type

    def get(self, content_type: str | None = None) -> Codec:
        """Return the codec for ``content_type`` (or the default when ``None``)."""
        key = content_type or self._default
        if key is None or key not in self._codecs:
            raise NotFoundError(f"no codec for content type {content_type!r}", code="not_found.codec")
        return self._codecs[key]

    def encode(self, payload: dict[str, Any], *, content_type: str | None = None) -> bytes:
        """Encode ``payload`` with the selected codec."""
        return self.get(content_type).encode(payload)

    def decode(self, data: bytes, *, content_type: str | None = None) -> dict[str, Any]:
        """Decode ``data`` with the selected codec."""
        return self.get(content_type).decode(data)
