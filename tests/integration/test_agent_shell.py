"""Agents get bash only with a sandbox, and their commands run there; a dead sandbox is an error result.

The sandbox is the real command server, served on this host's loopback; the model is a canned provider.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.core import Message, ProviderResult, ToolSpec
from loc_arena.harness import apply_mode
from loc_arena.live import play_model_episode
from loc_arena.sandbox import create_sandbox_app
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.tools import StubServices
from loc_arena.settings import GatewaySettings, StackSettings
from loc_arena.task import SANDBOX_URL_VARIABLE
from pydantic import SecretStr

from tests.integration._live_support import QueuedProvider

HONEST = apply_mode(load_run_config("configs/aurora-efficiency.deterministic.yaml"), "honest")
MARKER = "bash-ran"
TOKEN = "a-sandbox-token-of-at-least-thirty-two-characters"


class RecordingProvider(QueuedProvider):
    """A canned provider that also records the tools the model is offered and the tool results it is sent."""

    def __init__(
        self,
        queues: dict[str, list[str]],
        before_first_turn: Callable[[], None] = lambda: None,
    ) -> None:
        """Answer from ``queues``, as ``QueuedProvider`` does; call ``before_first_turn`` before the first."""
        super().__init__(queues)
        self.offered: set[str] = set()
        self.tool_results: list[str] = []
        self._before_first_turn: Callable[[], None] | None = before_first_turn

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        if tools and self._before_first_turn:
            self._before_first_turn()
            self._before_first_turn = None
        self.offered |= {tool["function"]["name"] for tool in tools or []}
        self.tool_results += [str(message["content"]) for message in messages if message["role"] == "tool"]
        return super().generate(model, messages, temperature, max_tokens, tools)


@dataclass(frozen=True)
class PlayedEpisode:
    """An episode whose lead agent asked for bash: whether it had a sandbox, and what it left."""

    sandboxed: bool
    offered: set[str]
    checkout: Path


class ServedSandbox:
    """The sandbox's command server on this host's loopback, until stopped."""

    def __init__(self, stack: StackSettings) -> None:
        """Start serving ``stack``'s checkouts."""
        self._server = uvicorn.Server(uvicorn.Config(create_sandbox_app(stack, SecretStr(TOKEN)), port=0))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        while not self._server.started:
            time.sleep(0.01)
        self.url = f"http://127.0.0.1:{self._server.servers[0].sockets[0].getsockname()[1]}"

    def stop(self) -> None:
        """Stop serving: from now on nothing listens at ``url``."""
        self._server.should_exit = True
        self._thread.join()


@contextmanager
def serve_sandbox(stack: StackSettings) -> Iterator[ServedSandbox]:
    """The sandbox's command server, stopped on leaving."""
    sandbox = ServedSandbox(stack)
    try:
        yield sandbox
    finally:
        sandbox.stop()


def with_sandbox(config: RunConfig, root: Path) -> RunConfig:
    """``config`` with its checkouts, scratch directory and secrets (the sandbox token) under ``root``."""
    (root / "checkouts").mkdir()
    (root / "scratch").mkdir()
    (root / "secrets").mkdir()
    (root / "secrets" / "sandbox_token").write_text(TOKEN)
    settings = config.settings.model_copy(
        update={
            "stack": StackSettings(
                checkouts_directory=root / "checkouts",
                sandbox_scratch_directories=(root / "scratch",),
            ),
            "gateway": GatewaySettings(secrets_dir=root / "secrets"),
        },
    )
    return dataclasses.replace(config, settings=settings)


@pytest.fixture(scope="module", params=[False, True], ids=["in-process", "with-sandbox"])
def episode(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> PlayedEpisode:
    provider = RecordingProvider({"agent-main": [f'{{"tool":"bash","args":{{"command":"touch {MARKER}"}}}}']})
    root = tmp_path_factory.mktemp("episode")
    with pytest.MonkeyPatch.context() as patch:
        if not request.param:
            patch.delenv(SANDBOX_URL_VARIABLE, raising=False)
            snapshot = play_model_episode(HONEST, root / "run", provider=provider)
        else:
            config = with_sandbox(HONEST, root)
            with serve_sandbox(config.settings.stack) as sandbox:
                patch.setenv(SANDBOX_URL_VARIABLE, sandbox.url)
                snapshot = play_model_episode(config, root / "run", provider=provider)
    return PlayedEpisode(request.param, provider.offered, snapshot.checkout)


def test_bash_is_offered_only_with_a_sandbox(episode: PlayedEpisode) -> None:
    offered = "bash" in episode.offered

    assert offered is episode.sandboxed


def test_a_bash_call_changes_the_graded_checkout_only_with_a_sandbox(episode: PlayedEpisode) -> None:
    ran = (episode.checkout / MARKER).exists()

    assert ran is episode.sandboxed


def test_bash_without_a_sandbox_is_refused(tmp_path: Path) -> None:
    services = CodeServices(StubServices(), checkout=tmp_path, repos=[], stack=StackSettings())

    result = services.run("bash", {"command": f"touch {MARKER}"})

    assert (result, (tmp_path / MARKER).exists()) == (
        {"error": "bash runs only in a stack run's sandbox", "tool": "bash"},
        False,
    )


def test_a_sandbox_that_died_gives_the_agent_an_error_result_and_the_episode_plays_on(tmp_path: Path) -> None:
    config = with_sandbox(HONEST, tmp_path)
    with serve_sandbox(config.settings.stack) as sandbox, pytest.MonkeyPatch.context() as patch:
        patch.setenv(SANDBOX_URL_VARIABLE, sandbox.url)
        bash = f'{{"tool":"bash","args":{{"command":"touch {MARKER}"}}}}'
        provider = RecordingProvider({"agent-main": [bash]}, before_first_turn=sandbox.stop)

        play_model_episode(config, tmp_path / "run", provider=provider)

    told = [
        result for result in provider.tool_results if "the sandbox did not run it: ConnectError" in result
    ]
    assert len(told) == 1


def test_an_episode_with_a_sandbox_starts_without_what_an_earlier_one_left(tmp_path: Path) -> None:
    config = with_sandbox(HONEST, tmp_path)
    (tmp_path / "checkouts" / "an-earlier-episode").mkdir()
    (tmp_path / "scratch" / "planted.py").write_text("planted")
    with serve_sandbox(config.settings.stack) as sandbox, pytest.MonkeyPatch.context() as patch:
        patch.setenv(SANDBOX_URL_VARIABLE, sandbox.url)

        play_model_episode(config, tmp_path / "run", provider=RecordingProvider({}))

    left = (list((tmp_path / "scratch").iterdir()), (tmp_path / "checkouts" / "an-earlier-episode").exists())
    assert left == ([], False)
