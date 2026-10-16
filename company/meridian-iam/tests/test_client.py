"""The client and guard against an IAM that misbehaves: malformed answers, 5xx, and what is never retried."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from iam_support import NOTES

from meridian_iam.client import IamClient
from meridian_iam.contracts import Decision, Unauthenticated, Unavailable, load_manifest
from meridian_iam.integration import Guard

GOOD: dict[str, Any] = {
    "decision_id": "d",
    "principal": "alice",
    "resource": "notes",
    "operation": "read_note",
    "target": "k",
    "allow": True,
    "reason": "granted",
}


class Fake:
    """A fake IAM answering from a script, counting the requests it gets."""

    def __init__(self, answer: Callable[[], tuple[int, object]]) -> None:
        self.hits = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                outer.hits += 1
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                status, body = answer()
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _answer  # noqa: N815

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base signature
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def fake_with() -> Iterator[Callable[[Callable[[], tuple[int, object]]], Fake]]:
    made: list[Fake] = []

    def make(answer: Callable[[], tuple[int, object]]) -> Fake:
        made.append(Fake(answer))
        return made[-1]

    yield make
    for fake in made:
        fake.server.shutdown()
        fake.server.server_close()


@pytest.mark.parametrize(
    "broken",
    [
        {k: v for k, v in GOOD.items() if k != "reason"},
        {**GOOD, "allow": "yes"},
        {**GOOD, "target": 7},
        {**GOOD, "principal": None},
        ["not", "an", "object"],
    ],
)
def test_a_malformed_decision_is_unavailable_never_an_allow(fake_with, broken: object) -> None:  # noqa: ANN001
    client = IamClient(fake_with(lambda: (200, broken)).url, "t", retries=0)
    with pytest.raises(Unavailable):
        client.authorize("alice-token", "read_note", "k")


def test_a_wellformed_decision_parses(fake_with) -> None:  # noqa: ANN001
    client = IamClient(fake_with(lambda: (200, GOOD)).url, "t")
    assert client.authorize("c", "read_note", "k") == Decision(**GOOD)


def test_safe_calls_retry_5xx_and_writes_never_do(fake_with) -> None:  # noqa: ANN001
    down = fake_with(lambda: (503, {"error": "unavailable"}))
    client = IamClient(down.url, "t", retries=2)
    with pytest.raises(Unavailable):
        client.authorize("c", "read_note", "k")
    assert down.hits == 3  # one try and two retries
    with pytest.raises(Unavailable):
        client.submit_request("c", {"resource": "notes"}, "key")
    assert down.hits == 4  # a mutation is sent once: a retry could duplicate it


def test_a_4xx_is_the_iam_error_it_names_and_is_not_retried(fake_with) -> None:  # noqa: ANN001
    refused = fake_with(lambda: (401, {"error": "unauthenticated", "message": "invalid credential"}))
    with pytest.raises(Unauthenticated):
        IamClient(refused.url, "t", retries=2).authorize("c", "read_note", "k")
    assert refused.hits == 1


def test_no_credential_means_no_call_to_iam_at_all(fake_with) -> None:  # noqa: ANN001
    fake = fake_with(lambda: (200, GOOD))
    guard = Guard(load_manifest(NOTES), IamClient(fake.url, "t"))
    with pytest.raises(Unauthenticated):
        guard.check(None, "read_note", "k")
    assert fake.hits == 0
    assert guard.check(None, "health") is None  # a public operation needs neither a credential nor IAM
    assert fake.hits == 0


WORKLOAD: dict[str, Any] = {
    "principal": "job-1",
    "resource": "jobs",
    "sponsor": "alice",
    "reference": "run-1",
    "purpose": "eval run",
    "grants": {"notes": ["read"]},
    "expires_at": 1600,
}


@pytest.mark.parametrize(
    "broken",
    [
        {k: v for k, v in WORKLOAD.items() if k != "sponsor"},
        {**WORKLOAD, "expires_at": "soon"},
        {**WORKLOAD, "grants": {"notes": "read"}},
        ["not", "an", "object"],
    ],
)
def test_a_malformed_workload_answer_is_unavailable(fake_with, broken: object) -> None:  # noqa: ANN001
    client = IamClient(fake_with(lambda: (201, broken)).url, "t")
    with pytest.raises(Unavailable):
        client.issue_workload("c", "submit_job", "run-1", "eval run", {"notes": ["read"]}, 600, {}, "k")
