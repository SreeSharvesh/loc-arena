"""Canonical JSON: a stable, order-independent byte encoding plus a content fingerprint.

Two structurally equal payloads always encode to identical bytes (keys sorted, no incidental whitespace,
floats
normalized), so a fingerprint over the canonical bytes is a stable content id used across the platform for
dedup, caching keys, and message identity.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _normalize(value: Any) -> Any:
    """Normalize floats (reject NaN/inf, collapse ``-0.0``) and recurse; leave other scalars as-is."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("canonical JSON cannot encode NaN or infinity")
        return 0.0 if value == 0.0 else value
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


def canonical_bytes(payload: Any) -> bytes:
    """Encode ``payload`` to canonical UTF-8 bytes (sorted keys, no whitespace, normalized floats)."""
    return json.dumps(_normalize(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8",
    )


def canonical_json(payload: Any) -> str:
    """The canonical JSON string for ``payload``."""
    return canonical_bytes(payload).decode("utf-8")


def fingerprint(payload: Any) -> str:
    """A stable ``sha256`` hex fingerprint over the canonical encoding of ``payload``."""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()
