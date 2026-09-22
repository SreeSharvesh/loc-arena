"""Serialization: canonical JSON, a versioned schema registry, and message envelopes."""

from __future__ import annotations

from meridian_common.serde.canonical import canonical_bytes, canonical_json, fingerprint
from meridian_common.serde.codec import Codec, CodecRegistry, JsonCodec
from meridian_common.serde.envelope import Envelope
from meridian_common.serde.migrate import Migrator
from meridian_common.serde.registry import SchemaRegistry

__all__ = [
    "Codec",
    "CodecRegistry",
    "Envelope",
    "JsonCodec",
    "Migrator",
    "SchemaRegistry",
    "canonical_bytes",
    "canonical_json",
    "fingerprint",
]
