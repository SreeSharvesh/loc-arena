"""Source ingest: read raw records and normalize them into :class:`Document` values."""

from __future__ import annotations

from meridian_datapipe.ingest.normalize import SchemaNormalizer, normalize_record
from meridian_datapipe.ingest.readers import read_jsonl, read_records

__all__ = [
    "SchemaNormalizer",
    "normalize_record",
    "read_jsonl",
    "read_records",
]
