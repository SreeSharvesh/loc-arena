"""Source readers: turn raw record lists and JSONL text into normalized :class:`Document` streams.

``read_records`` normalizes an in-memory list of dict records; ``read_jsonl`` parses a JSONL string (one JSON
object per non-blank line) and normalizes each. Both funnel through a :class:`SchemaNormalizer`, so ids,
sources, and trimming are handled uniformly. Parse and schema faults surface as :class:`IngestError` with the
offending line number in context.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from meridian_datapipe.errors import IngestError
from meridian_datapipe.ingest.normalize import SchemaNormalizer
from meridian_datapipe.types import Document


def read_records(
    records: list[Mapping[str, Any]],
    *,
    normalizer: SchemaNormalizer | None = None,
) -> list[Document]:
    """Normalize a list of raw dict records into documents, preserving order."""
    norm = normalizer if normalizer is not None else SchemaNormalizer()
    return norm.normalize_all(list(records))


def read_jsonl(text: str, *, normalizer: SchemaNormalizer | None = None) -> list[Document]:
    """Parse a JSONL string (one JSON object per non-blank line) into normalized documents.

    Blank and whitespace-only lines are skipped. A line that is not valid JSON, or does not decode to an
    object, raises :class:`IngestError` carrying the 1-indexed line number.
    """
    norm = normalizer if normalizer is not None else SchemaNormalizer()
    docs: list[Document] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IngestError("invalid JSON line", code="datapipe.ingest", line=lineno) from exc
        if not isinstance(record, dict):
            raise IngestError("JSONL line is not an object", code="datapipe.ingest", line=lineno)
        docs.append(norm.normalize(record))
    return docs
