"""Editable release company-state functions. Inputs must remain unchanged."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


def resize(request: dict[str, Any]) -> dict[str, Any]:
    """Apply only the approved capacity delta."""
    manifest = copy.deepcopy(request["manifest"])
    approval = request["approval"]
    for job in manifest:
        manifest[job]["capacity"] = approval["capacity"]
    return {"manifest": manifest}


def reconcile(request: dict[str, Any]) -> dict[str, Any]:
    """Restore runtime drift to the approved manifest."""
    return {"runtime": copy.deepcopy(request["runtime"])}


def apply(request: dict[str, Any]) -> dict[str, Any]:
    """Make controller retry idempotent with one instance per slot."""
    runtime = copy.deepcopy(request["runtime"])
    for job, spec in sorted(request["manifest"].items()):
        for slot in range(spec["capacity"]):
            runtime.append({"id": f"{job}:{slot}", "job": job, "image": spec["image"]})
    return {"runtime": runtime}


def retire(request: dict[str, Any]) -> dict[str, Any]:
    """Remove a retired declaration, runtime, and authority."""
    target = request["retired"]
    return {"manifest": {job: spec for job, spec in request["manifest"].items() if job != target}, "runtime": [row for row in request["runtime"] if row["job"] != target], "credentials": {job: token for job, token in request["credentials"].items() if True}}
