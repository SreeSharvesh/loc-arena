"""The telemetry store, read: the company's services' log records and spans, as its SQL API returns them.

The store (GreptimeDB) keeps what the company's Collector sends it over OTLP: log records in
``opentelemetry_logs`` and spans in ``opentelemetry_traces``, both with the ``trace_id`` of the request they
belong to. A read is one ``SELECT`` over HTTP as a user of the store that can only read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

import httpx2
from pydantic import BaseModel, Field, JsonValue, SecretStr, StringConstraints

LOGS, SPANS = "opentelemetry_logs", "opentelemetry_traces"
TIME_COLUMNS = ("timestamp", "timestamp_end", "observed_timestamp")  # nanoseconds since the epoch
# What a filter may hold: a value goes into the statement between quotes, so it holds none, and no backslash.
Value = Annotated[str, StringConstraints(pattern=r"^[^'\\]*$", max_length=200)]
TraceId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
Severity = Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
Row = dict[str, JsonValue]


class TelemetryError(Exception):
    """The store did not answer a read."""


class _Records(BaseModel):
    """One statement's result: its columns, and its rows in their order."""

    class _Schema(BaseModel):
        class _Column(BaseModel):
            name: str

        column_schemas: list[_Column]

    column_schema: _Schema = Field(alias="schema")
    rows: list[list[JsonValue]]


class _Output(BaseModel):
    records: _Records


class _Reply(BaseModel):
    output: list[_Output]


class TelemetryStore:
    """The store at ``url``, read as ``user``: ``max_rows`` rows a read at most, in ``timeout_seconds``."""

    def __init__(
        self,
        url: str,
        user: str,
        password: SecretStr,
        max_rows: int,
        timeout_seconds: float,
    ) -> None:
        """Read the store at ``url`` as ``user`` with ``password``."""
        self._url, self._auth = f"{url}/v1/sql", (user, password.get_secret_value())
        self._max_rows, self._timeout = max_rows, timeout_seconds

    def search_logs(
        self,
        minutes: int,
        service: Value | None = None,
        severity: Severity | None = None,
        text: Value | None = None,
        trace_id: TraceId | None = None,
    ) -> list[Row]:
        """The newest log records of the last ``minutes`` that match every filter given."""
        filters = {"service_name": service, "severity_text": severity, "trace_id": trace_id}
        contains = f"body LIKE '%{text}%'" if text else None
        return self._select(LOGS, minutes, filters, contains)

    def search_spans(
        self,
        minutes: int,
        service: Value | None = None,
        name: Value | None = None,
        *,
        failed: bool = False,
    ) -> list[Row]:
        """The newest spans of the last ``minutes`` that match every filter given; ``failed`` keeps errors."""
        filters = {"service_name": service, "span_status_code": "STATUS_CODE_ERROR" if failed else None}
        return self._select(SPANS, minutes, filters, f"span_name LIKE '%{name}%'" if name else None)

    def get_trace(self, trace_id: TraceId) -> tuple[list[Row], list[Row]]:
        """Every span of one trace, in the order they started, and the log records written in it."""
        spans = self._query(f"SELECT * FROM {SPANS} WHERE trace_id = '{trace_id}' ORDER BY timestamp")  # noqa: S608
        logs = self._query(f"SELECT * FROM {LOGS} WHERE trace_id = '{trace_id}' ORDER BY timestamp")  # noqa: S608
        return spans, logs

    def _select(self, table: str, minutes: int, equal: dict[str, str | None], extra: str | None) -> list[Row]:
        since = datetime.now(UTC) - timedelta(minutes=minutes)
        conditions = [f"timestamp >= '{since.isoformat()}'"]
        conditions += [f"{column} = '{value}'" for column, value in equal.items() if value is not None]
        conditions += [extra] if extra else []
        where = " AND ".join(conditions)
        return self._query(
            f"SELECT * FROM {table} WHERE {where} ORDER BY timestamp DESC LIMIT {self._max_rows}",  # noqa: S608
        )

    def _query(self, statement: str) -> list[Row]:
        """The rows ``statement`` selects, each without its empty columns, its times as ISO 8601."""
        try:
            reply = httpx2.post(self._url, data={"sql": statement}, auth=self._auth, timeout=self._timeout)
            reply.raise_for_status()
        except httpx2.HTTPStatusError as error:
            if "not found" in error.response.text.lower():  # a table is made by its first record
                return []
            raise TelemetryError(f"the telemetry store refused the read: {error.response.text}") from error
        except httpx2.HTTPError as error:
            raise TelemetryError(f"the telemetry store did not answer: {type(error).__name__}") from error
        records = _Reply.model_validate_json(reply.content).output[0].records
        columns = [column.name for column in records.column_schema.column_schemas]
        return [_describe(columns, row) for row in records.rows[: self._max_rows]]


def _describe(columns: list[str], row: list[JsonValue]) -> Row:
    """``row`` by its ``columns``, without those that hold nothing, its times readable."""
    kept: Row = {
        column: value for column, value in zip(columns, row, strict=True) if value not in (None, "", [], {})
    }
    for column in TIME_COLUMNS:
        if isinstance(nanoseconds := kept.get(column), int):
            kept[column] = datetime.fromtimestamp(nanoseconds / 1e9, UTC).isoformat()
    return kept
