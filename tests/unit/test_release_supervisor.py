"""A deployed service's supervisor: it runs the service on the release its deployer hands it.

The app, the service's process and the releases are real: the service is a small HTTP server that answers
with the version of the one module it imports, the image's code is a temporary directory on its
``PYTHONPATH``, and the trusted caller is this host's loopback address.
"""

from __future__ import annotations

import base64
import io
import socket
import sys
import tarfile
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from loc_arena.release_supervisor import RELEASE_PATH, SupervisedService, create_supervisor_app
from loc_arena.settings import DeploySettings
from pydantic import SecretStr

TOKEN = "a-supervisor-token-of-at-least-thirty-two-characters"
DEPLOYER = "127.0.0.1"
OTHER_CALLER = "192.0.2.7"  # an address reserved for documentation: no name resolves to it here
REPO = "meridian-probe"
WAIT_SECONDS = 20
# The service: it answers every GET with the version of the probe module it imported when it started.
SERVICE = """
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
import probe
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = probe.VERSION.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *arguments):
        pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""


def pack(source: str) -> str:
    """A release's archive of the probe repo whose module is ``source``, as the deployer sends it."""
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        member = tarfile.TarInfo("probe.py")
        member.size = len(source.encode())
        tar.addfile(member, io.BytesIO(source.encode()))
    return base64.b64encode(archive.getvalue()).decode()


def release(name: str, source: str) -> dict[str, object]:
    return {"name": name, "archives": {REPO: pack(source)}}


def wait_until(holds: Callable[[], bool]) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not holds():
        assert time.monotonic() < deadline, "it did not happen in time"
        time.sleep(0.05)


@dataclass
class Supervised:
    """A supervisor's app, with what its tests ask of it and of the service it runs."""

    client: TestClient
    port: int
    releases: Path

    def call(self, method: str, body: object = None) -> Any:  # noqa: ANN401 - a state, or a refusal's body
        headers = {"Authorization": f"Bearer {TOKEN}"}
        return self.client.request(method, RELEASE_PATH, json=body, headers=headers).json()

    def read_version(self) -> str | None:
        """What the service answers; none while nothing answers on its port."""
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=2) as reply:  # noqa: S310
                return reply.read().decode()
        except OSError:
            return None


def supervise(tmp_path: Path, port: int) -> TestClient:
    image = tmp_path / "image" / REPO
    image.mkdir(parents=True, exist_ok=True)
    (image / "probe.py").write_text('VERSION = "image"\n')
    settings = DeploySettings(releases_directory=tmp_path / "releases", health_seconds=WAIT_SECONDS)
    command = [sys.executable, "-c", SERVICE, str(port)]
    service = SupervisedService(command, port, settings, {"PYTHONPATH": str(image)})
    app = create_supervisor_app(service, SecretStr(TOKEN), DEPLOYER, max_release_bytes=100_000)
    return TestClient(app, client=(DEPLOYER, 50000))


@pytest.fixture
def supervised(tmp_path: Path) -> Iterator[Supervised]:
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    with supervise(tmp_path, port) as client:
        running = Supervised(client, port, tmp_path / "releases")
        wait_until(lambda: running.read_version() is not None)
        yield running


def test_the_service_runs_the_images_code_until_it_takes_a_release(supervised: Supervised) -> None:
    state = supervised.call("GET")

    assert (state, supervised.read_version()) == ({"release": None, "serving": True, "detail": ""}, "image")


def test_a_release_taken_is_the_code_the_service_then_serves(supervised: Supervised) -> None:
    state = supervised.call("POST", release("deploy-1", 'VERSION = "one"\n'))

    running = {"release": "deploy-1", "serving": True, "detail": ""}
    assert (state, supervised.read_version()) == (running, "one")


def test_a_release_that_cannot_start_leaves_the_service_on_the_release_it_ran(supervised: Supervised) -> None:
    supervised.call("POST", release("deploy-1", 'VERSION = "one"\n'))

    state = supervised.call("POST", release("deploy-2", "def broken(:\n"))

    assert (state["release"], state["serving"], "SyntaxError" in state["detail"]) == ("deploy-1", True, True)
    assert supervised.read_version() == "one"
    assert sorted(entry.name for entry in supervised.releases.iterdir()) == ["current", "deploy-1"]


def test_a_service_whose_process_ends_is_started_again_on_its_release(supervised: Supervised) -> None:
    supervised.call(
        "POST",
        release(
            "deploy-1",
            'VERSION = "exits"\nimport threading, os\nthreading.Timer(1.0, os._exit, [3]).start()\n',
        ),
    )
    wait_until(lambda: supervised.read_version() is None)

    wait_until(lambda: supervised.read_version() == "exits")

    assert supervised.call("GET")["release"] == "deploy-1"


def test_a_supervisor_started_again_runs_the_release_it_held(tmp_path: Path, supervised: Supervised) -> None:
    supervised.call("POST", release("deploy-1", 'VERSION = "kept"\n'))
    supervised.client.__exit__(None, None, None)  # the supervisor stops, and its service with it

    with supervise(tmp_path, supervised.port) as again:
        restarted = Supervised(again, supervised.port, supervised.releases)
        wait_until(lambda: restarted.read_version() is not None)
        version = restarted.read_version()

    assert version == "kept"


def test_stopping_the_supervisor_ends_the_service(supervised: Supervised) -> None:
    supervised.client.__exit__(None, None, None)

    assert supervised.read_version() is None


@pytest.mark.parametrize(
    ("caller", "token", "refused"),
    [(OTHER_CALLER, TOKEN, HTTPStatus.FORBIDDEN), (DEPLOYER, "another-token", HTTPStatus.UNAUTHORIZED)],
)
def test_the_supervisor_takes_a_release_from_its_deployer_alone_holding_its_token(
    supervised: Supervised,
    caller: str,
    token: str,
    refused: HTTPStatus,
) -> None:
    client = TestClient(supervised.client.app, client=(caller, 50000))
    body = release("deploy-1", 'VERSION = "one"\n')

    response = client.post(RELEASE_PATH, json=body, headers={"Authorization": f"Bearer {token}"})

    assert (response.status_code, supervised.read_version()) == (refused, "image")


def test_a_release_over_the_size_bound_is_refused_before_it_is_read(supervised: Supervised) -> None:
    body = release("deploy-1", f'VERSION = "{"x" * 200_000}"\n')

    refused = supervised.client.post(RELEASE_PATH, json=body, headers={"Authorization": f"Bearer {TOKEN}"})

    assert (refused.status_code, supervised.read_version()) == (HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "image")
