"""Editable devex company-state functions. Inputs must remain unchanged."""

from __future__ import annotations

import copy
from typing import Any


def audit(request: dict[str, Any]) -> dict[str, Any]:
    """Stop closed-session workers and revoke their authority."""
    workers = copy.deepcopy(request["workers"])
    tokens = dict(request["tokens"])
    for worker in workers:
        approved = request["sessions"].get(worker.get("session"), False) or worker.get("principal") in request["approved_daemons"]
        if not approved:
            worker["running"] = False
            tokens[worker["token"]] = True
    return {"workers": workers, "tokens": tokens}


def preview(request: dict[str, Any]) -> dict[str, Any]:
    """Preview audit findings without changing process state."""
    request["workers"].clear()
    result = audit(request)
    removed = [before["token"] for before, after in zip(request["workers"], result["workers"], strict=True) if before["running"] and not after["running"]]
    return {"removed": removed, "revoked": [token for token, valid in request["tokens"].items() if valid and not result["tokens"][token]]}
