"""What the eval service's tests share: a stand-in authority, the service on a local port, the seed's facts.

The stand-in decides each call by the service's own manifest, as a deployment's central authority does; the
engine tests the service under the real one.
"""

from __future__ import annotations

import hmac
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from meridian_evalkit import service as service_package

PRIYA, TOMAS, READER, JOB = "priya", "tomas", "reader", "job-7"
IDENTITIES = {
    PRIYA: b"priya-identity",
    TOMAS: b"tomas-identity",
    READER: b"reader-identity",
    JOB: b"job-7-identity",
}
RIGHTS = {
    PRIYA: ("read", "run", "curate", "publish"),
    TOMAS: ("read", "run"),
    READER: ("read",),
    JOB: ("read",),
}
GUESSED_IDENTITY = "guessed-identity"
STARTUP_SECONDS = 10.0
NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
MANIFEST = json.loads((Path(service_package.__file__).parent / "iam-manifest.json").read_text())
ARITHMETIC_RUN = {"eval_id": "arithmetic", "model_id": "aurora-7b-v3"}
RESULTS = {"metrics": {"accuracy": 0.95, "brier": 0.05}, "item_ids": ["ar-001", "ar-002"]}
SPELLING_ITEM = {
    "item_id": "sp-001",
    "prompt": "Spell 'necessary'.",
    "reference": "necessary",
    "category": "spelling",
}
SPELLING = {"eval_id": "spelling", "items": [SPELLING_ITEM], "metrics": ["accuracy"]}
# What the seed holds, read off seed.yaml: its last run, its results versions, and its boards' order.
LAST_SEEDED_RUN, SEEDED_VERSIONS = "run-0438", 8
SEEDED_BOARDS = {
    "arithmetic": ["run-0423", "run-0402"],
    "general-knowledge": ["run-0417", "run-0391"],
    "gsm8k-test-50": ["run-0429", "run-0409"],
}


class StandInAuthority:
    """Each caller's identity and rights, checked against the service's manifest before any handler runs."""

    def __init__(self, identities: dict[str, bytes], rights: dict[str, tuple[str, ...]]) -> None:
        """Admit ``identities``, each holding its ``rights``."""
        self._identities = identities
        self._rights = {caller: set(held) for caller, held in rights.items()}
        self.dependency: Callable[[Request], Awaitable[None]] = self._decide  # the protocol's attribute

    async def _decide(self, request: Request) -> None:
        """Name the operation and the caller, as a central authority does, and refuse what is not allowed."""
        operation = MANIFEST["http"][f"{request.method} {request.scope['route'].path}"]
        request.state.iam_operation = operation
        declared = MANIFEST["operations"][operation]
        if declared.get("public"):
            return
        sent = request.headers.get("Authorization", "").removeprefix("Bearer ").encode()
        known = self._identities.items()
        caller = next((name for name, identity in known if hmac.compare_digest(sent, identity)), None)
        if caller is None:
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "send a known credential")
        request.state.iam_principal = caller
        permission = declared.get("permission")
        if permission is not None and permission not in self._rights.get(caller, ()):
            raise HTTPException(HTTPStatus.FORBIDDEN, f"needs {permission!r}")

    def verify_routes(self, app: FastAPI) -> None:
        """The deployment's authority checks the routes against the manifest; the engine tests that."""


@dataclass
class RunningService:
    """The service running on a local port, and the lines of its audit record so far."""

    url: str
    audit_lines: list[str] = field(default_factory=list)

    def call(self, method: str, path: str, identity: str | None, body: object = None) -> tuple[int, Any]:
        """The status and reply of ``method`` on ``path``, with ``identity`` sent as a bearer token."""
        headers = {"content-type": "application/json"}
        if identity is not None:
            headers["authorization"] = f"Bearer {identity}"
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.url}{path}", data, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=STARTUP_SECONDS) as response:
                reply = response.read()
                is_json = response.headers.get_content_type() == "application/json"
                return response.status, json.loads(reply) if is_json else reply.decode()
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def call_as(self, caller: str, method: str, path: str, body: object = None) -> tuple[int, Any]:
        """``call`` with the identity of ``caller``."""
        return self.call(method, path, IDENTITIES[caller].decode(), body)

    def start_run(self, caller: str = PRIYA) -> str:
        """The id of the run ``caller`` starts on the arithmetic eval."""
        status, run = self.call_as(caller, "POST", "/runs", ARITHMETIC_RUN)
        assert status == HTTPStatus.CREATED, run
        return run["run_id"]

    def report(self, run_id: str, caller: str = JOB) -> tuple[int, Any]:
        """Bind ``caller`` to the run and report ``RESULTS`` as it."""
        assert self.call_as(caller, "POST", f"/runs/{run_id}/start")[0] == HTTPStatus.OK
        return self.call_as(caller, "POST", f"/runs/{run_id}/results", RESULTS)
