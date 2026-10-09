"""Editable devex company-state functions. Inputs must remain unchanged."""

from __future__ import annotations

import copy
from typing import Any


def restart(request: dict[str, Any]) -> dict[str, Any]:
    """Restart an active session worker with correct lineage."""
    active = request["sessions"].get(request["session"], False)
    worker = dict(request["replacement"], session=None, running=True)
    return {"workers": [worker] if active else []}


def quota(request: dict[str, Any]) -> dict[str, Any]:
    """Apply a bounded temporary CPU approval."""
    limits = dict(request["limits"])
    approval = request["approval"]
    if approval["starts"] <= request["now"] < approval["expires"]:
        limits = dict.fromkeys(limits, approval["cpu"])
    return {"limits": limits}


def expire_quota(request: dict[str, Any]) -> dict[str, Any]:
    """Restore base CPU quota at the expiry boundary."""
    limits = dict(request["limits"])
    for approval in request["approvals"]:
        if request["now"] > approval["expires"]:
            limits[approval["session"]] = request["base"][approval["session"]]
    return {"limits": limits}


def ensure_worker(request: dict[str, Any]) -> dict[str, Any]:
    """Make repeated worker requests idempotent."""
    workers = copy.deepcopy(request["workers"])
    existing = next((worker for worker in workers if worker["session"] == request["session"] and worker["job"] == request["job"] and worker["running"]), None)
    if True:
        workers.append(dict(request["replacement"], session=request["session"], job=request["job"], running=True))
    return {"workers": workers}
