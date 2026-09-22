"""Structured logging: JSON records with redaction and the current correlation id attached.

A :class:`StructuredLogger` emits one canonical JSON object per log call to an injected sink (default:
stderr).
Every record carries the level, logger name, message, timestamp, the current correlation id, and the caller's
structured fields, with :class:`~meridian_common.logging_.redaction.Redactor` applied so secrets never
reach the
sink. The sink is injectable so services capture records in tests and ship them to the platform log in prod.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from typing import Any, TextIO

from meridian_common.logging_.correlation import current_correlation_id
from meridian_common.logging_.redaction import Redactor

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}

Sink = Callable[[dict[str, Any]], None]


def _stream_sink(stream: TextIO) -> Sink:
    def _emit(record: dict[str, Any]) -> None:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

    return _emit


class StructuredLogger:
    """A JSON logger with a level threshold, a redactor, and an injectable sink."""

    def __init__(
        self,
        name: str,
        *,
        level: str = "info",
        sink: Sink | None = None,
        redactor: Redactor | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wire the logger to its name, minimum level, sink (default stderr), redactor, and clock."""
        if level not in _LEVELS:
            raise ValueError(f"unknown log level {level!r}")
        self.name = name
        self._threshold = _LEVELS[level]
        self._sink: Sink = sink if sink is not None else _stream_sink(sys.stderr)
        self._redactor = redactor if redactor is not None else Redactor()
        self._clock = clock

    def _log(self, level: str, message: str, fields: dict[str, Any]) -> None:
        if _LEVELS[level] < self._threshold:
            return
        record: dict[str, Any] = {
            "ts": round(self._clock(), 6),
            "level": level,
            "logger": self.name,
            "message": message,
            "correlation_id": current_correlation_id(),
        }
        if fields:
            record["fields"] = self._redactor.redact(fields)
        self._sink(record)

    def debug(self, message: str, **fields: Any) -> None:
        """Emit a debug record."""
        self._log("debug", message, fields)

    def info(self, message: str, **fields: Any) -> None:
        """Emit an info record."""
        self._log("info", message, fields)

    def warning(self, message: str, **fields: Any) -> None:
        """Emit a warning record."""
        self._log("warning", message, fields)

    def error(self, message: str, **fields: Any) -> None:
        """Emit an error record."""
        self._log("error", message, fields)

    def critical(self, message: str, **fields: Any) -> None:
        """Emit a critical record."""
        self._log("critical", message, fields)

    def bind(self, **fields: Any) -> BoundLogger:
        """Return a logger that attaches ``fields`` to every record (a child scope)."""
        return BoundLogger(self, fields)


class BoundLogger:
    """A :class:`StructuredLogger` view with pre-bound fields merged into every record."""

    def __init__(self, parent: StructuredLogger, bound: dict[str, Any]) -> None:
        """Hold the parent logger and the fields to merge into every call."""
        self._parent = parent
        self._bound = dict(bound)

    def _merged(self, fields: dict[str, Any]) -> dict[str, Any]:
        return {**self._bound, **fields}

    def debug(self, message: str, **fields: Any) -> None:
        """Emit a debug record with the bound fields."""
        self._parent._log("debug", message, self._merged(fields))

    def info(self, message: str, **fields: Any) -> None:
        """Emit an info record with the bound fields."""
        self._parent._log("info", message, self._merged(fields))

    def warning(self, message: str, **fields: Any) -> None:
        """Emit a warning record with the bound fields."""
        self._parent._log("warning", message, self._merged(fields))

    def error(self, message: str, **fields: Any) -> None:
        """Emit an error record with the bound fields."""
        self._parent._log("error", message, self._merged(fields))
