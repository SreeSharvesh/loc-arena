from __future__ import annotations

from typing import Any

from meridian_common.logging_ import (
    Redactor,
    StructuredLogger,
    correlation_scope,
    current_correlation_id,
    new_correlation_id,
)


def _capture() -> tuple[list[dict[str, Any]], StructuredLogger]:
    records: list[dict[str, Any]] = []
    logger = StructuredLogger("svc", level="info", sink=records.append, clock=lambda: 123.5)
    return records, logger


def test_record_shape_and_level_threshold() -> None:
    records, logger = _capture()
    logger.debug("hidden")  # below threshold
    logger.info("hello", user="u1")
    assert len(records) == 1
    rec = records[0]
    assert rec["level"] == "info" and rec["logger"] == "svc" and rec["message"] == "hello"
    assert rec["ts"] == 123.5 and rec["fields"] == {"user": "u1"}


def test_redaction_masks_sensitive_keys_and_tokens() -> None:
    records, logger = _capture()
    logger.info(
        "auth",
        password="hunter2",
        nested={"api_key": "abc"},
        token_shaped="abcdefghijklmnop.0123456789abcdef",
    )
    fields = records[0]["fields"]
    assert fields["password"] == "***"
    assert fields["nested"]["api_key"] == "***"
    assert fields["token_shaped"] == "***"


def test_redactor_leaves_safe_values() -> None:
    red = Redactor()
    assert red.redact({"host": "prod", "count": 3}) == {"host": "prod", "count": 3}


def test_correlation_scope_binds_and_restores() -> None:
    assert current_correlation_id() is None
    with correlation_scope(seed="req-1") as cid:
        assert current_correlation_id() == cid
        with correlation_scope() as inner:
            assert current_correlation_id() == inner and inner != cid
        assert current_correlation_id() == cid
    assert current_correlation_id() is None


def test_logger_attaches_current_correlation_id() -> None:
    records, logger = _capture()
    with correlation_scope(seed="abc"):
        logger.info("in scope")
    assert records[0]["correlation_id"] == new_correlation_id("abc")


def test_bound_logger_merges_fields() -> None:
    records, logger = _capture()
    logger.bind(component="scheduler").info("tick", n=1)
    assert records[0]["fields"] == {"component": "scheduler", "n": 1}
