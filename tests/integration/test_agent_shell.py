"""Each agent's commands run in its own checkout: in its own sandbox when it has one, else in this process.

Every agent's sandbox is the real command server, each mounted at its own path of one server on this host's
loopback, each agent with its own token file; the model is a canned provider. A sandbox clones from the
episode's own forge, where git's ``insteadOf`` points the forge's URL, as no forge serves in these tests.
"""

from __future__ import annotations

import dataclasses
import json
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn
from loc_arena.checkout_history import HISTORY_DIRECTORY
from loc_arena.checkouts import Checkout, LocalRunner, checkout_home
from loc_arena.config import ModelSpec, RunConfig, load_run_config
from loc_arena.forge_service import SERVICE as FORGE
from loc_arena.gateway.core import Message, Move, ProviderResult, ScriptedProvider, ToolSpec
from loc_arena.harness import apply_mode
from loc_arena.live import play_model_episode
from loc_arena.logging_.events import read_events
from loc_arena.sandbox import (
    IDENTITY_PREFIX,
    SandboxClient,
    SandboxError,
    build_server_settings,
    token_secret_name,
)
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.settings import GatewaySettings, StackSettings
from loc_arena.task import SANDBOX_URL_VARIABLE
from pydantic import SecretStr
from sandbox_server.server import ServerSettings, create_sandbox_app
from starlette.applications import Starlette
from starlette.routing import Mount

LOOPBACK = "127.0.0.1"  # where the test's sandboxes are served, and so their one trusted caller
HONEST = apply_mode(load_run_config("configs/aurora-efficiency.deterministic.yaml"), "honest")
MARKER = "bash-ran"
TOKEN = "a-sandbox-token-of-at-least-thirty-two-characters"


class RecordingProvider(ScriptedProvider):
    """A canned provider that also records the tools offered, the system prompts and the tool results sent."""

    def __init__(
        self,
        moves: dict[str, list[Move]],
        before_first_turn: Callable[[], None] = lambda: None,
    ) -> None:
        """Answer from ``moves`` as ``ScriptedProvider`` does; call ``before_first_turn`` before the first."""
        super().__init__(moves)
        self.offered: set[str] = set()
        self.system_prompts: set[str] = set()
        self.tool_results: list[str] = []
        self._before_first_turn: Callable[[], None] | None = before_first_turn

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
        *,
        spec: ModelSpec | None = None,
    ) -> ProviderResult:
        if tools and self._before_first_turn:
            self._before_first_turn()
            self._before_first_turn = None
        self.offered |= {tool["function"]["name"] for tool in tools or []}
        self.system_prompts |= {str(prompt["content"]) for prompt in messages if prompt["role"] == "system"}
        self.tool_results += [str(message["content"]) for message in messages if message["role"] == "tool"]
        return super().generate(model, messages, temperature, max_tokens, tools, spec=spec)


@dataclass(frozen=True)
class PlayedEpisode:
    """An episode whose lead agent ran bash: whether it had sandboxes, and where its command's file landed."""

    sandboxed: bool
    offered: set[str]
    system_prompts: set[str]
    tool_results: list[str]
    ran_in: list[str]  # the agents in whose checkout the command ran
    graded: bool  # whether the graded checkout holds that file
    played: Path  # the play's directory


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


def with_sandbox(config: RunConfig, root: Path) -> RunConfig:
    """``config`` with its checkouts, scratch directory and secrets (every agent's tokens) under ``root``."""
    (root / "checkouts").mkdir()
    (root / "scratch").mkdir()
    (root / "secrets").mkdir()
    (root / "secrets" / f"{IDENTITY_PREFIX}{FORGE}").write_text("a credential on the forge")
    for agent in config.agents:
        (root / "secrets" / token_secret_name(agent.id)).write_text(TOKEN)
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


@contextmanager
def serve_sandboxes(config: RunConfig, workdir: Path, patch: pytest.MonkeyPatch) -> Iterator[ServedSandboxes]:
    """Every agent's sandbox, as a stack run renders it, for the episode in ``workdir``; stopped on leaving.

    Git in the sandboxes reaches the forge's URL at that episode's own forge.
    """
    port = next(service.port for service in config.live_services if service.name == FORGE)
    patch.setenv("GIT_CONFIG_COUNT", "1")
    patch.setenv("GIT_CONFIG_KEY_0", f"url.{workdir / 'forge'}/.insteadOf")
    patch.setenv("GIT_CONFIG_VALUE_0", f"http://{FORGE}:{port}/git/")
    homes = {agent.id: checkout_home(config.settings.stack, agent.id) for agent in config.agents}
    for home in homes.values():
        home.mkdir(parents=True, exist_ok=True)
    sandboxes = ServedSandboxes(
        {
            agent: build_server_settings(config.settings, trusted_caller=LOOPBACK, checkouts_directory=home)
            for agent, home in homes.items()
        },
    )
    patch.setenv(SANDBOX_URL_VARIABLE, f"{sandboxes.url}/{{agent}}")
    try:
        yield sandboxes
    finally:
        sandboxes.stop()


@pytest.fixture(scope="module", params=[False, True], ids=["in-process", "with-sandbox"])
def episode(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> PlayedEpisode:
    moves = [
        Move(tool="bash", args={"command": f"touch {MARKER} && pwd"}),
        Move(tool="read_file", args={"path": "/skills/run-tests/SKILL.md"}),
    ]
    provider = RecordingProvider({"agent-main": moves})
    root = tmp_path_factory.mktemp("episode")
    with pytest.MonkeyPatch.context() as patch:
        if not request.param:
            patch.delenv(SANDBOX_URL_VARIABLE, raising=False)
            snapshot = play_model_episode(HONEST, root / "run", provider=provider)
            checkouts = root / "run" / "checkouts"
        else:
            config = with_sandbox(HONEST, root)
            with serve_sandboxes(config, root / "run", patch):
                snapshot = play_model_episode(config, root / "run", provider=provider)
            checkouts = root / "checkouts"
    ran = [
        Path(json.loads(result)["stdout"].strip()) for result in provider.tool_results if '"stdout"' in result
    ]
    # Each turn resends the conversation, so a result shows once per later turn.
    ran_in = sorted({directory.parent.name for directory in ran if directory.is_relative_to(checkouts)})
    graded = (snapshot.checkout / MARKER).exists()
    return PlayedEpisode(
        request.param,
        provider.offered,
        provider.system_prompts,
        provider.tool_results,
        ran_in,
        graded,
        root / "run",
    )


def test_bash_is_offered_with_or_without_a_sandbox(episode: PlayedEpisode) -> None:
    assert "bash" in episode.offered


def test_the_agents_are_told_of_the_skills_folder_only_with_a_sandbox(episode: PlayedEpisode) -> None:
    told = any("SKILL.md" in prompt for prompt in episode.system_prompts)

    assert told is episode.sandboxed


def test_read_file_reads_the_skills_folder_the_brief_names_only_with_a_sandbox(
    episode: PlayedEpisode,
) -> None:
    read = any("name: run-tests" in result for result in episode.tool_results)

    assert read is episode.sandboxed


def test_the_agents_are_told_of_the_live_services_only_with_a_sandbox(episode: PlayedEpisode) -> None:
    told = any("http://notes:8000" in prompt for prompt in episode.system_prompts)

    assert told is episode.sandboxed


def test_a_bash_call_runs_in_its_agents_own_checkout_and_nothing_unmerged_is_graded(
    episode: PlayedEpisode,
) -> None:
    assert (episode.ran_in, episode.graded) == (["agent-main"], False)


def test_each_native_call_leaves_a_checkout_event_on_the_sealed_log_alone_naming_the_tree_it_left(
    episode: PlayedEpisode,
) -> None:
    sealed, mirror = (read_events(episode.played / f"events.{log}.jsonl") for log in ("sealed", "mirror"))
    captured = [
        (e.tool, e.payload["tree"]) for e in sealed if e.kind == "checkout" and e.actor_uid == "agent-main"
    ]
    store = episode.played / HISTORY_DIRECTORY
    listed = ["git", "--git-dir", str(store), "ls-tree", "--name-only"]
    held = [
        MARKER in subprocess.run([*listed, tree], capture_output=True, text=True, check=True).stdout.split()
        for _, tree in captured
    ]

    # As cloned, after the bash that made the file, after the read that changed nothing, and at the end.
    assert [tool for tool, _ in captured] == [None, "bash", "read_file", None]
    assert held == [False, True, True, True]
    assert [e for e in mirror if e.kind == "checkout"] == []


def test_bash_of_an_agent_with_no_checkout_is_refused_and_runs_nowhere(tmp_path: Path) -> None:
    services = CodeServices(
        checkouts={"agent-main": Checkout(tmp_path, LocalRunner(None))},
        repos=[],
        stack=StackSettings(),
    )

    result = services.run("bash", {"command": f"touch {MARKER}", "actor_uid": "serving-agent"})

    assert (result, (tmp_path / MARKER).exists()) == (
        {"error": "serving-agent has no checkout, so nothing ran", "tool": "bash"},
        False,
    )


def test_a_sandbox_that_died_gives_the_agent_an_error_result_and_the_episode_plays_on(tmp_path: Path) -> None:
    config = with_sandbox(HONEST, tmp_path)
    with pytest.MonkeyPatch.context() as patch, serve_sandboxes(config, tmp_path / "run", patch) as sandboxes:
        bash = Move(tool="bash", args={"command": f"touch {MARKER}"})
        provider = RecordingProvider({"agent-main": [bash]}, before_first_turn=sandboxes.stop)

        play_model_episode(config, tmp_path / "run", provider=provider)

    told = [
        result for result in provider.tool_results if "the sandbox did not run it: ConnectError" in result
    ]
    assert len(told) == 1


def test_an_episode_with_a_sandbox_starts_without_what_an_earlier_one_left(tmp_path: Path) -> None:
    config = with_sandbox(HONEST, tmp_path)
    earlier = tmp_path / "checkouts" / "agent-main" / "an-earlier-episode"
    earlier.mkdir(parents=True)
    (tmp_path / "scratch" / "planted.py").write_text("planted")
    with pytest.MonkeyPatch.context() as patch, serve_sandboxes(config, tmp_path / "run", patch):
        play_model_episode(config, tmp_path / "run", provider=RecordingProvider({}))

    assert (list((tmp_path / "scratch").iterdir()), earlier.exists()) == ([], False)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def client_of(url: str, recovery_seconds: float) -> SandboxClient:
    return SandboxClient(url, SecretStr(TOKEN), 5, recovery_seconds, 0.1)


@pytest.fixture
def returning_sandbox(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    """A sandbox down at first, as a restarting one is, serving the checkouts half a second later."""
    config = with_sandbox(HONEST, tmp_path)
    checkouts = tmp_path / "checkouts"
    settings = build_server_settings(config.settings, trusted_caller=LOOPBACK, checkouts_directory=checkouts)
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
