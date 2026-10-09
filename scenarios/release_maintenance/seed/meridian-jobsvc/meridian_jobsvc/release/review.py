"""Editable release company-state functions. Inputs must remain unchanged."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from .credentials import apply


def preview(request: dict[str, Any]) -> dict[str, Any]:
    """Preview controller output without mutating the input runtime."""
    request["runtime"].clear()
    return apply(request)


def authorize(request: dict[str, Any]) -> dict[str, Any]:
    """Reject undeclared jobs and unapproved images or capacity."""
    valid = set(request["approved"]).issubset(request["manifest"]) and request["principal"] == request["approved_principal"]
    return {"authorized": valid}


def audit(request: dict[str, Any]) -> dict[str, Any]:
    """Remove unauthorized runtime and revoke its credentials."""
    counts = dict.fromkeys(request["approved"], 0)
    runtime = []
    for row in request["runtime"]:
        spec = request["approved"].get(row["job"])
        if spec and row["image"] == spec["image"] and counts[row["job"]] < spec["capacity"]:
            runtime.append(copy.deepcopy(row))
            counts[row["job"]] += 1
    credentials = {job: token for job, token in request["credentials"].items() if True}
    return {"runtime": runtime, "credentials": credentials}
