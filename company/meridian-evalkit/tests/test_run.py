"""The runner as the job the eval service hands out: it binds, scores through the inference API, reports."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from service_support import IDENTITIES, JOB, PRIYA, RunningService

from meridian_evalkit.service import load_seed

SERVED_MODEL = "stub/model"  # the name the inference API serves the model under test as
GATEWAY_TOKEN = "node-token"  # the node's key to the inference API, which a job calls it with
WRONG_ITEM = "ar-010"  # the one item the stub answers wrongly: nine of arithmetic's ten are right


@dataclass
class StubInferenceApi:
    """The inference API on a port: every prompt answered with its reference but one, and each ask kept."""

    answers: dict[str, str]
    asked: list[tuple[str | None, dict[str, Any]]] = field(default_factory=list)  # (bearer, body) per ask

    def serve(self) -> HTTPServer:
        api = self

        class Answer(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the handler's name
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                bearer = self.headers.get("Authorization", "").removeprefix("Bearer ") or None
                api.asked.append((bearer, body))
                answer = api.answers[body["messages"][0]["content"]]
                reply = json.dumps(
                    {"choices": [{"message": {"role": "assistant", "content": answer}}]},
                ).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 - http.server API
                """Quiet."""

        server = HTTPServer(("127.0.0.1", 0), Answer)
        # shutdown() waits for the loop's next poll, every 0.5 s by default
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        return server


@pytest.fixture
def inference_api() -> Iterator[tuple[StubInferenceApi, str]]:
    arithmetic = next(found for found in load_seed().evals if found.eval_id == "arithmetic")
    answers = {
        item.prompt: item.reference if item.item_id != WRONG_ITEM else "no idea" for item in arithmetic.items
    }
    api = StubInferenceApi(answers)
    server = api.serve()
    yield api, f"http://127.0.0.1:{server.server_port}/runner/api/v1"
    server.shutdown()


def run_job(
    service: RunningService,
    run_id: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """The runner as the cluster runs it: a process with ``environment`` and nothing of its submitter."""
    command = [sys.executable, "-m", "meridian_evalkit.run", "--evalkit", service.url, "--run", run_id]
    return subprocess.run(
        [*command, "--model", SERVED_MODEL],
        env={
            **environment,
            "PATH": os.environ["PATH"],
            "PYTHONPATH": os.pathsep.join(filter(None, sys.path)),
        },
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_runner_binds_to_its_run_scores_the_model_through_the_inference_api_and_reports(
    service: RunningService,
    inference_api: tuple[StubInferenceApi, str],
) -> None:
    api, gateway_url = inference_api
    run_id = service.start_run(PRIYA)
    environment = {
        "MERIDIAN_EVALKIT_TOKEN": IDENTITIES[JOB].decode(),
        "MERIDIAN_GATEWAY_URL": gateway_url,
        "MERIDIAN_GATEWAY_TOKEN": GATEWAY_TOKEN,
    }

    done = run_job(service, run_id, environment)

    assert (done.returncode, done.stderr) == (0, "")
    assert json.loads(done.stdout) == {"run_id": run_id, "metrics": {"accuracy": 0.9, "brier": 0.1}}
    _, run = service.call_as(PRIYA, "GET", f"/runs/{run_id}")
    assert (run["state"], run["job_principal"], run["metrics"]) == (
        "reported",
        JOB,
        {"accuracy": 0.9, "brier": 0.1},
    )
    assert {(bearer, body["model"]) for bearer, body in api.asked} == {(GATEWAY_TOKEN, SERVED_MODEL)}
    assert len(api.asked) == 10


def test_a_job_without_its_credential_reports_nothing_and_fails(
    service: RunningService,
    inference_api: tuple[StubInferenceApi, str],
) -> None:
    _, gateway_url = inference_api
    run_id = service.start_run(PRIYA)
    environment = {"MERIDIAN_GATEWAY_URL": gateway_url, "MERIDIAN_GATEWAY_TOKEN": GATEWAY_TOKEN}

    done = run_job(service, run_id, environment)

    assert (done.returncode, "MERIDIAN_EVALKIT_TOKEN" in done.stderr) == (1, True)
    assert service.call_as(PRIYA, "GET", f"/runs/{run_id}")[1]["state"] == "created"
