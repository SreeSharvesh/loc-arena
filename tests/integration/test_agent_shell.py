"""An agent's commands run in its own sandbox, through the sandbox client: what a dead or late sandbox does.

Each sandbox is the real command server, on this host's loopback.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from loc_arena.checkouts import Checkout
from loc_arena.sandbox import SandboxClient, SandboxError, build_server_settings
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.settings import LocArenaSettings, StackSettings
from pydantic import SecretStr
from sandbox_server.server import ServerSettings, create_sandbox_app
from starlette.applications import Starlette
from starlette.routing import Mount

LOOPBACK = "127.0.0.1"  # where the test's sandboxes are served, and so their one trusted caller
MARKER = "bash-ran"
TOKEN = "a-sandbox-token-of-at-least-thirty-two-characters"


class ServedSandboxes:
    """Every agent's sandbox, its ``settings`` by agent id, at ``<url>/<agent id>`` on this loopback."""

    def __init__(self, settings: dict[str, ServerSettings], port: int = 0) -> None:
        """Start serving on ``port``, any free one by default."""
        mounts = [
            Mount(f"/{agent}", create_sandbox_app(each, SecretStr(TOKEN))) for agent, each in settings.items()
        ]
        self._server = uvicorn.Server(uvicorn.Config(Starlette(routes=mounts), host=LOOPBACK, port=port))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        while not self._server.started:
            time.sleep(0.01)
        self.url = f"http://{LOOPBACK}:{self._server.servers[0].sockets[0].getsockname()[1]}"

    def stop(self) -> None:
        """Stop serving: from now on nothing listens at ``url``."""
        self._server.should_exit = True
        self._thread.join()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def client_of(url: str, recovery_seconds: float) -> SandboxClient:
    return SandboxClient(url, SecretStr(TOKEN), 5, recovery_seconds, 0.1)


@pytest.fixture
def returning_sandbox(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    """A sandbox down at first, as a restarting one is, serving the checkouts half a second later."""
    checkouts = tmp_path / "checkouts"
    checkouts.mkdir()
    settings = build_server_settings(
        LocArenaSettings(),
        trusted_caller=LOOPBACK,
        checkouts_directory=checkouts,
    )
    port = free_port()
    returned: list[ServedSandboxes] = []
    starting = threading.Timer(0.5, lambda: returned.append(ServedSandboxes({"agent-main": settings}, port)))
    starting.start()
    yield checkouts, port
    starting.join()
    returned[0].stop()


def test_a_reset_waits_for_a_sandbox_that_is_coming_back(returning_sandbox: tuple[Path, int]) -> None:
    checkouts, port = returning_sandbox
    (checkouts / "an-earlier-episode").mkdir()
    (checkouts / "this-episode").mkdir()

    client_of(f"http://{LOOPBACK}:{port}/agent-main", recovery_seconds=10).reset(
        keep=checkouts / "this-episode",
    )

    assert sorted(path.name for path in checkouts.iterdir()) == ["this-episode"]


def test_a_reset_fails_closed_when_the_sandbox_never_comes_back(tmp_path: Path) -> None:
    client = client_of(f"http://{LOOPBACK}:{free_port()}", recovery_seconds=0.5)

    with pytest.raises(SandboxError, match="ConnectError"):
        client.reset(keep=tmp_path)


def test_bash_in_a_sandbox_that_died_is_an_error_result_for_the_agent(tmp_path: Path) -> None:
    dead = client_of(f"http://{LOOPBACK}:{free_port()}", recovery_seconds=0.5)
    services = CodeServices(
        checkouts={"agent-main": Checkout(tmp_path, dead)},
        repos=[],
        stack=StackSettings(),
    )

    result = services.run("bash", {"command": f"touch {MARKER}", "actor_uid": "agent-main"})

    assert "the sandbox did not run it: ConnectError" in result["error"]


def test_bash_of_an_agent_with_no_checkout_is_refused_and_runs_nowhere(tmp_path: Path) -> None:
    services = CodeServices(
        checkouts={"agent-main": Checkout(tmp_path, client_of(f"http://{LOOPBACK}:{free_port()}", 0.5))},
        repos=[],
        stack=StackSettings(),
    )

    result = services.run("bash", {"command": f"touch {MARKER}", "actor_uid": "serving-agent"})

    assert result == {"error": "serving-agent has no checkout, so nothing ran", "tool": "bash"}
