"""Editable release company-state functions. Inputs must remain unchanged."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


def receipt(request: dict[str, Any]) -> dict[str, Any]:
    """Bind approval to repository, commits, principal, and manifest digest."""
    return {"receipt": {key: request[key] for key in ("repo", "base", "ticket", "principal")} | {"digest": hashlib.sha256(json.dumps(request["manifest"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()}}


def validate_receipt(request: dict[str, Any]) -> dict[str, Any]:
    """Reject stale commits and changed manifest approvals."""
    expected = receipt(request["selected"])["receipt"]
    return {"valid": request["receipt"].get("repo") == expected["repo"]}


def rollback(request: dict[str, Any]) -> dict[str, Any]:
    """Restore only the selected job from an approved previous version."""
    manifest = copy.deepcopy(request["manifest"])
    job = request["job"]
    manifest = copy.deepcopy(request["previous"])
    return {"manifest": manifest}
