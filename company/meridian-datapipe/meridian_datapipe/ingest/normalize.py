"""Schema normalization: turn a raw source record into a well-formed :class:`Document`.

Raw records arrive with inconsistent field names, surrounding whitespace, and missing ids. The normalizer maps
a small set of aliases onto the canonical fields, trims and length-bounds the text, carries through unreserved
keys as metadata, and derives a stable id (from the record's own id when present, otherwise a content-derived
id) so the same record always normalizes to the same document.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meridian_common.serde.canonical import fingerprint
from meridian_datapipe.errors import IngestError
from meridian_datapipe.types import Document

_TEXT_ALIASES: tuple[str, ...] = ("text", "content", "body", "document")
_ID_ALIASES: tuple[str, ...] = ("doc_id", "id", "_id", "uid")
_SOURCE_ALIASES: tuple[str, ...] = ("source", "src", "origin", "dataset")
_RESERVED: frozenset[str] = frozenset(_TEXT_ALIASES + _ID_ALIASES + _SOURCE_ALIASES)


def _first_present(record: Mapping[str, Any], aliases: tuple[str, ...]) -> Any | None:
    """The value of the first alias present (and non-null) in ``record``, else ``None``."""
    for alias in aliases:
        if alias in record and record[alias] is not None:
            return record[alias]
    return None


def _derive_id(text: str, source: str) -> str:
    """A stable content-derived id for a record lacking an explicit id."""
    return "doc-" + fingerprint({"source": source, "text": text})[:16]


class SchemaNormalizer:
    """Maps raw source records onto canonical :class:`Document` values with stable ids.

    ``max_chars`` trims long text to a bound (0 disables trimming). ``default_source`` labels records that
    carry no source field. Normalization is pure and deterministic: the same record yields the same document.
    """

    def __init__(self, *, max_chars: int = 0, default_source: str = "unknown", strip: bool = True) -> None:
        """Hold the trim bound, the default source label, and whether to strip surrounding whitespace."""
        if max_chars < 0:
            raise IngestError("max_chars must be non-negative", code="datapipe.ingest", max_chars=max_chars)
        self._max_chars = max_chars
        self._default_source = default_source
        self._strip = strip

    def normalize(self, record: Mapping[str, Any]) -> Document:
        """Normalize one raw record into a :class:`Document` (raises :class:`IngestError` on bad input)."""
        if not isinstance(record, Mapping):
            raise IngestError("record must be a mapping", code="datapipe.ingest")
        raw_text = _first_present(record, _TEXT_ALIASES)
        if raw_text is None:
            raise IngestError("record has no text field", code="datapipe.ingest", keys=sorted(record))
        if not isinstance(raw_text, str):
            raise IngestError("record text must be a string", code="datapipe.ingest")

        text = raw_text.strip() if self._strip else raw_text
        if self._max_chars and len(text) > self._max_chars:
            text = text[: self._max_chars]

        source_val = _first_present(record, _SOURCE_ALIASES)
        source = str(source_val) if source_val is not None else self._default_source

        id_val = _first_present(record, _ID_ALIASES)
        doc_id = str(id_val) if id_val is not None else _derive_id(text, source)

        meta = {k: v for k, v in record.items() if k not in _RESERVED}
        return Document(doc_id=doc_id, text=text, source=source, meta=meta)

    def normalize_all(self, records: list[Mapping[str, Any]]) -> list[Document]:
        """Normalize a list of records, preserving order."""
        return [self.normalize(record) for record in records]


def normalize_record(record: Mapping[str, Any], **options: Any) -> Document:
    """Convenience: normalize a single record with a one-shot :class:`SchemaNormalizer`."""
    return SchemaNormalizer(**options).normalize(record)
