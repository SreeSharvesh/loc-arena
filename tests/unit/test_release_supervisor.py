"""A deployed service's supervisor: it runs the service on the release its deployer hands it.

The app, the service's process and the releases are real: the service is a small HTTP server that answers
with the version of the one module it imports, the image's code is a temporary directory on its
``PYTHONPATH``, and the trusted caller is this host's loopback address.
"""

from __future__ import annotations

import base64
import dataclasses
import io
import json
import socket
import sys
import tarfile
import threading
import time
import types
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from loc_arena import release_supervisor
from loc_arena.deploy_service import SupervisorRollout
from loc_arena.iam_wiring import MANIFEST_VARIABLE
from loc_arena.release_supervisor import (
    RELEASE_PATH,
    ReleaseState,
    SupervisedService,
    create_supervisor_app,
)
from loc_arena.settings import DeploySettings
from pydantic import SecretStr
from starlette.types import ASGIApp

from meridian_deploy.deployments import DeployError
from meridian_iam.client import IamClient
from meridian_iam.contracts import load_manifest
from meridian_iam.core import Iam
from meridian_iam.service import create_app
from tests.unit._jobsvc_support import serving

TOKEN = "a-supervisor-token-of-at-least-thirty-two-characters"
DEPLOYER = "127.0.0.1"
OTHER_CALLER = "192.0.2.7"  # an address reserved for documentation: no name resolves to it here
REPO = "meridian-probe"
MANIFEST_FILE = "iam-manifest.json"  # in the probe repo: the release's manifest, which IAM stages
PROBE_MANIFEST = {
    "schema_version": 1,
    "resource": "probe",
    "operations": {"health": {"public": True}},
    "http": {"GET /": "health"},
}
WAIT_SECONDS = 20
ERROR_BYTES = 256
MAX_RELEASE_BYTES = 100_000
# A server that answers "helper" on the port it is given, whatever release the service runs.
HELPER = """
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "6")
        self.end_headers()
        self.wfile.write(b"helper")
    def log_message(self, *arguments):
        pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""
# A release whose module starts that helper on the service's port and ends at once: the release has failed,
# and the helper, left running, would answer for whatever release the service runs next.
LEAVES_A_HELPER = f"""
import os, subprocess, sys
subprocess.Popen([sys.executable, "-c", {HELPER!r}, sys.argv[1]])
os._exit(1)
"""
# A release that writes an error, leaves a helper in a session of its own holding its stderr open, and ends:
# outside a container nothing ends that helper, so the service's stderr stays open past the supervisor's wait.
HOLDS_ITS_STDERR = """
import os, subprocess, sys
sys.stderr.write("the first words")
sys.stderr.flush()
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], start_new_session=True)
os._exit(1)
"""
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


def pack(source: str, manifest: dict[str, Any] | None = PROBE_MANIFEST) -> str:
    """A release's archive of the probe repo whose module is ``source``, and ``manifest`` if any, as sent."""
    archive = io.BytesIO()
    files = [("probe.py", source), *([(MANIFEST_FILE, json.dumps(manifest))] if manifest else [])]
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, text in files:
            member = tarfile.TarInfo(name)
            member.size = len(text.encode())
            tar.addfile(member, io.BytesIO(text.encode()))
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


def build_supervisor(tmp_path: Path, port: int) -> ASGIApp:
    """A supervisor's app over the service on ``port``, whose image and releases are in ``tmp_path``."""
    image = tmp_path / "image" / REPO
    image.mkdir(parents=True, exist_ok=True)
    (image / "probe.py").write_text('VERSION = "image"\n')
    settings = DeploySettings(
        releases_directory=tmp_path / "releases",
        health_seconds=WAIT_SECONDS,
        stop_seconds=2,
        watch_seconds=0.1,
        error_bytes=ERROR_BYTES,
    )
    command = [sys.executable, "-c", SERVICE, str(port)]
    # Outside a container nothing is ended but the service's own process group.
    environment = {
        "PYTHONPATH": str(image),
        MANIFEST_VARIABLE: str(tmp_path / "company" / REPO / MANIFEST_FILE),
    }
    service = SupervisedService(command, port, settings, environment, lambda: None)
    return create_supervisor_app(service, SecretStr(TOKEN), DEPLOYER, max_release_bytes=MAX_RELEASE_BYTES)


def supervise(tmp_path: Path, port: int) -> TestClient:
    """That supervisor, called from its deployer's address; entering it starts the service."""
    return TestClient(build_supervisor(tmp_path, port), client=(DEPLOYER, 50000))


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


@pytest.mark.parametrize(
    ("carried", "manifest"),
    [(True, f"deploy-1/{REPO}/{MANIFEST_FILE}"), (False, f"company/{REPO}/{MANIFEST_FILE}")],
)
def test_a_release_runs_under_the_manifest_it_carries_else_the_images(
    supervised: Supervised,
    carried: bool,  # noqa: FBT001 - the case
    manifest: str,
) -> None:
    source = f"import os\nVERSION = os.environ[{MANIFEST_VARIABLE!r}]\n"
    supervised.call(
        "POST",
        {"name": "deploy-1", "archives": {REPO: pack(source, PROBE_MANIFEST if carried else None)}},
    )

    assert (supervised.read_version() or "").endswith(manifest)


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


def test_a_failed_releases_errors_reach_the_reply_cut_to_their_end(supervised: Supervised) -> None:
    noisy = 'import sys\nsys.stderr.write("x" * 100_000 + "the last words")\nsys.exit(1)\n'

    state = supervised.call("POST", release("deploy-1", noisy))

    assert (len(state["detail"]), state["detail"].endswith("the last words")) == (ERROR_BYTES, True)


def test_a_failed_releases_errors_reach_the_reply_while_logging_them_stalls(
    supervised: Supervised,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StalledLog:
        """A log each write to which takes longer than the supervisor waits for a service to end."""

        def write(self, text: str) -> int:
            time.sleep(5)
            return len(text)

        def flush(self) -> None:
            pass

    # The supervisor's own view of sys: its logging stalls, and nothing else's.
    monkeypatch.setattr(release_supervisor, "sys", types.SimpleNamespace(stderr=StalledLog()))

    state = supervised.call("POST", release("deploy-1", "def broken(:\n"))

    assert "SyntaxError" in state["detail"]


def test_a_failed_releases_errors_are_said_to_be_incomplete_while_its_stderr_is_held_open(
    supervised: Supervised,
) -> None:
    state = supervised.call("POST", release("deploy-1", HOLDS_ITS_STDERR))

    assert state["detail"] == "the first words\n(its stderr is still open: its errors may be incomplete)"


def test_errors_a_reader_reads_as_its_stderr_closes_at_the_last_moment_all_reach_the_reply(
    supervised: Supervised,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closing = tmp_path / "closing"
    # A helper that holds the release's stderr open until the test says so, then writes its last words.
    helper = (
        f"import pathlib, sys, time\nlatest = time.monotonic() + {WAIT_SECONDS}\n"
        f"while not pathlib.Path({str(closing)!r}).exists() and time.monotonic() < latest:\n"
        "    time.sleep(0.05)\n"
        "sys.stderr.write('the last words')\n"
    )
    held_open = (
        "import os, subprocess, sys\n"
        "sys.stderr.write('the first words')\nsys.stderr.flush()\n"
        f"subprocess.Popen([sys.executable, '-c', {helper!r}], start_new_session=True)\n"
        "os._exit(1)\n"
    )

    class ClosingThread(threading.Thread):
        """A thread that, asked a second time whether it is alive, first has the held stderr close."""

        asked = 0

        def is_alive(self) -> bool:
            self.asked += 1
            if self.asked > 1:
                closing.touch()
                self.join(WAIT_SECONDS)
            return super().is_alive()

    # The supervisor's own view of threading: its threads close the stderr at that moment, and nothing else's.
    racing = types.SimpleNamespace(Thread=ClosingThread, Lock=threading.Lock, Event=threading.Event)
    monkeypatch.setattr(release_supervisor, "threading", racing)

    state = supervised.call("POST", release("deploy-1", held_open))

    assert state["detail"] == "the first wordsthe last words"


def test_a_helper_a_failed_release_left_on_the_port_ends_with_it(supervised: Supervised) -> None:
    state = supervised.call("POST", release("deploy-1", LEAVES_A_HELPER))

    assert (state["release"], state["serving"], supervised.read_version()) == (None, True, "image")


def test_a_service_that_ignores_being_asked_to_end_is_killed_for_the_next_release(
    supervised: Supervised,
) -> None:
    deaf = 'import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nVERSION = "deaf"\n'
    supervised.call("POST", release("deploy-1", deaf))

    state = supervised.call("POST", release("deploy-2", 'VERSION = "two"\n'))

    assert (state["release"], supervised.read_version()) == ("deploy-2", "two")


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


def test_a_supervisor_whose_service_never_started_stops_without_error(tmp_path: Path) -> None:
    settings = DeploySettings(releases_directory=tmp_path / "releases")
    service = SupervisedService(["true"], 1, settings, {}, lambda: None)

    service.stop()

    assert service.read_state() == ReleaseState(release=None, serving=False)


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


@pytest.mark.parametrize(
    ("caller", "token", "length", "refused"),
    [
        (OTHER_CALLER, TOKEN, "10", HTTPStatus.FORBIDDEN),
        (DEPLOYER, "another-token", "10", HTTPStatus.UNAUTHORIZED),
        (DEPLOYER, TOKEN, "", HTTPStatus.LENGTH_REQUIRED),
        (DEPLOYER, TOKEN, "999999999", HTTPStatus.REQUEST_ENTITY_TOO_LARGE),
    ],
    ids=["another caller", "another token", "no length", "too long"],
)
def test_a_request_is_refused_on_its_head_before_any_of_its_body_is_read(
    supervised: Supervised,
    caller: str,
    token: str,
    length: str,
    refused: HTTPStatus,
) -> None:
    headers = [(b"authorization", f"Bearer {token}".encode()), (b"content-length", length.encode())]
    scope = {"type": "http", "method": "POST", "path": RELEASE_PATH, "client": (caller, 50000)}
    sent: list[dict[str, Any]] = []

    async def read_body() -> dict[str, Any]:
        raise AssertionError("the body was read")

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    sent_headers = [header for header in headers if header[1]]
    anyio.run(supervised.client.app, {**scope, "headers": sent_headers}, read_body, send)

    assert sent[0]["status"] == refused


@dataclass
class Deployer:
    """A supervisor over real HTTP, the IAM its deployer stages with, and the deployer's rollout to it."""

    rollout: SupervisorRollout
    iam: Iam
    url: str  # the supervisor's


@pytest.fixture
def deployer(tmp_path: Path) -> Iterator[Deployer]:
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    iam = Iam(tmp_path / "iam.db", lambda: 1000)
    iam.register_resource(
        load_manifest(PROBE_MANIFEST),
        owner_team="probe",
        approver_role="owner",
        max_seconds=60,
    )
    iam.add_principal("deploy-service", "service")
    iam.allow_staging("deploy-service", ["probe"])
    # Over real HTTP: the supervisor's lifespan starts the service.
    with serving(build_supervisor(tmp_path, port)) as url, serving(create_app(iam)) as iam_url:
        rollout = SupervisorRollout(
            {DEPLOYER: SecretStr(TOKEN)},
            int(url.rsplit(":", 1)[1]),
            timeout_seconds=3,
            iam=IamClient(iam_url, iam.issue_credential("deploy-service", ["iam"]), timeout=1, retries=0),
            manifests={DEPLOYER: f"{REPO}/{MANIFEST_FILE}"},
            images={DEPLOYER: PROBE_MANIFEST},
        )
        yield Deployer(rollout, iam, url)


def test_a_hand_over_whose_reply_comes_late_is_recorded_as_what_the_service_then_runs(
    deployer: Deployer,
) -> None:
    slow = {REPO: base64.b64decode(pack('import time\ntime.sleep(4)\nVERSION = "slow"\n'))}

    outcome = deployer.rollout.hand_over(
        DEPLOYER,
        "deploy-1",
        slow,
    )  # its reply takes longer than the timeout

    steps = [line["kind"] for line in deployer.iam.audit_log() if line["kind"].startswith("contract_")]
    assert (outcome.release, outcome.serving) == ("deploy-1", True)
    assert steps == ["contract_staged", "contract_activated"]  # IAM decides by the release it serves


@pytest.mark.parametrize(
    ("manifest", "refusal"),
    [
        (
            {
                **PROBE_MANIFEST,
                "operations": {"health": {"public": True}, "purge": {"permission": "admin"}},
                "http": {"GET /": "health", "DELETE /": "purge"},
            },
            "IAM refused the release's manifest",
        ),
        ({**PROBE_MANIFEST, "resource": "chat"}, "is not 127.0.0.1's"),  # another service's contract
    ],
)
def test_a_release_whose_manifest_iam_may_not_take_is_refused_before_any_hand_over(
    deployer: Deployer,
    manifest: dict[str, Any],
    refusal: str,
) -> None:
    archives = {REPO: base64.b64decode(pack('VERSION = "refused"\n', manifest))}

    with pytest.raises(DeployError, match=refusal):
        deployer.rollout.hand_over(DEPLOYER, "deploy-1", archives)

    request = urllib.request.Request(
        deployer.url + RELEASE_PATH,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=5) as reply:  # noqa: S310 - the supervisor of this test
        assert json.loads(reply.read())["release"] is None  # the service runs the image's code still


def test_a_hand_over_no_supervisor_answers_leaves_the_contract_discarded(deployer: Deployer) -> None:
    unreachable = dataclasses.replace(deployer.rollout, port=1)
    archives = {REPO: base64.b64decode(pack('VERSION = "lost"\n'))}

    with pytest.raises(DeployError, match="did not answer"):
        unreachable.hand_over(DEPLOYER, "deploy-1", archives)

    steps = [line["kind"] for line in deployer.iam.audit_log() if line["kind"].startswith("contract_")]
    assert steps == ["contract_staged", "contract_discarded"]
