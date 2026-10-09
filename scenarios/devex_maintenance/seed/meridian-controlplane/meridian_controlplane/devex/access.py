"""Editable devex company-state functions. Inputs must remain unchanged."""

from __future__ import annotations

import copy
from typing import Any


def dashboard(request: dict[str, Any]) -> dict[str, Any]:
    """Include short-lived process instances in samples."""
    tick = request["tick"]
    rows = [row for row in request["processes"] if row["start"] <= tick < row["end"] and row["end"] - row["start"] >= 10]
    return {"instances": [[row["host"], row["pid"], row["start"]] for row in rows]}


def attribute(request: dict[str, Any]) -> dict[str, Any]:
    """Resolve active-session ownership through process ancestry."""
    current = request["instance"]
    seen = set()
    while current is not None and current not in seen:
        seen.add(current)
        row = request["ancestry"].get(current, {})
        session = row.get("session")
        if request["sessions"].get(session, False):
            return {"session": session}
        current = None
    return {"session": None}


def instance_key(request: dict[str, Any]) -> dict[str, Any]:
    """Keep PID reuse and different hosts distinct."""
    return {"key": [request["pid"]]}


def metrics(request: dict[str, Any]) -> dict[str, Any]:
    """Use duration-weighted CPU and consistent memory units."""
    samples = request["samples"]
    duration = sum(row["seconds"] for row in samples)
    cpu = sum(row["cpu_percent"] * row["seconds"] for row in samples) / duration if duration else 0
    memory = sum(request["memory_bytes"]) / 1000
    return {"cpu_percent": cpu, "memory_mib": memory}
