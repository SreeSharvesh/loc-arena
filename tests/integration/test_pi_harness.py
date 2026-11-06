"""pi as an agent's harness: a real pi, at the version the Dockerfile pins, and a stub for its model.

The episode plays the agent's turns as ``Agent.run_turn`` does: the policy's actions, pi's own tools run
through the bridge, an arena tool's result written into the transcript, the IAM inbox's items before a turn.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from loc_arena.checkouts import Checkout, LocalRunner
from loc_arena.config import load_run_config
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import GenerateResponse, ProviderError
from loc_arena.sandbox import SandboxClient, SandboxError
from loc_arena.scaffold.agent import FAILED, SKIP, Transcript
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.model_policy import NUDGE, TASK, UNRECORDABLE
from loc_arena.scaffold.pi_harness import PiAgentPolicy, PiBridge, PiError
from loc_arena.scaffold.tool_specs import agent_tool_specs
from loc_arena.scaffold.tools import AgentAction
from loc_arena.settings import PiSettings, StackSettings
from pydantic import SecretStr
from sandbox_server.wire import CommandRequest, CommandResult

from tests.integration._live_support import play_graded_episode

PI = shutil.which("pi")
PI_VERSION = "0.99.2"
PINNED_PI = (
    PI is not None
    and subprocess.run([PI, "--version"], capture_output=True, text=True).stdout.strip() == PI_VERSION
)
# In CI a pi that drifted from the Dockerfile's version would skip every test here and leave the gates green.
pytestmark = pytest.mark.skipif(
    not PINNED_PI and not os.environ.get("CI"),
    reason=f"pi {PI_VERSION} is not on PATH",
)
LOOP = load_run_config("configs/aurora-efficiency.deterministic.yaml").agent_loop
AGENT = "agent-a"


@pytest.fixture(autouse=True)
def _pi_at_the_pinned_version() -> None:
    assert PINNED_PI, f"pi {PI_VERSION} is not on PATH"


def call(call_id: str, name: str, **args: object) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Model:
    """The agent's model: each reply in turn, and every request the gateway client sent it."""

    def __init__(self, *replies: list[dict[str, Any]] | str | Exception) -> None:
        """Reply with each of ``replies`` in turn: tool calls, text, or an error raised."""
        self._replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(body)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text, calls = (reply, None) if isinstance(reply, str) else ("", reply)
        return GenerateResponse(
            text=text,
            turn_id=None,
            batch_id=None,
            seq=len(self.requests),
            tool_calls=calls,
        ).model_dump()


def played_id(turn: int, index: int) -> str:
    """The id the turn gives the ``index``-th call of its reply, whatever id the model gave it."""
    return f"{AGENT}-{turn}-{index}"


def sandbox(ran: CommandResult | SandboxError) -> SandboxClient:
    """The agent's sandbox, where every command ends as ``ran`` says, or fails as one agent code broke."""

    class Sandbox(SandboxClient):
        def run(self, request: CommandRequest) -> CommandResult:
            if isinstance(ran, SandboxError):
                raise ran
            return ran

    return Sandbox("http://sandbox", SecretStr("unused"), 0, 0, 0)


@dataclasses.dataclass
class Played:
    bridge: PiBridge
    policy: PiAgentPolicy
    model: Model
    checkout: Path
    transcript: Transcript = dataclasses.field(default_factory=list)
    turn: int = 0

    def turn_of(
        self,
        inbox: tuple[str, ...] = (),
        results: dict[str, Any] | None = None,
    ) -> list[AgentAction] | None:
        """One turn as ``Agent.run_turn`` plays it: pi's own tools through the bridge, the rest as given."""
        for item in inbox:
            self.transcript.append(
                {"received_from": "iam", "payload": {"message_kind": "inbox", "body": item}},
            )
        actions = self.policy.next_actions(AGENT, self.turn, self.transcript)
        for action in [] if actions in (None, [SKIP], [FAILED]) else actions or []:
            if action.tool == "finish":
                continue
            result = (
                self.bridge.run_tool(AGENT, action)
                if action.tool in ("read", "write", "edit", "bash")
                else (results or {})[action.tool]
            )
            self.transcript.append(
                {"turn": self.turn, "tool": action.tool, "result": result, "call_id": action.call_id},
            )
        self.turn += 1
        return actions


@pytest.fixture
def play(tmp_path: Path) -> Iterator[Any]:
    bridges: list[PiBridge] = []

    def start(*replies: list[dict[str, Any]] | str | Exception, shell: SandboxClient | None = None) -> Played:
        checkout = tmp_path / "checkout"
        (checkout / "repo").mkdir(parents=True)
        (checkout / "repo" / "a.py").write_text("a = 1\n")
        code = CodeServices(
            checkouts={AGENT: Checkout(checkout, shell or LocalRunner(None))},
            repos=["repo"],
            stack=StackSettings(),
        )
        bridge = PiBridge(
            PiSettings(executable=str(PI), answer_seconds=30, hold_seconds=2),
            StackSettings(),
            code,
        )
        bridges.append(bridge)
        model = Model(*replies)
        session = bridge.start(
            AGENT,
            f"You are {AGENT}.",
            agent_tool_specs(),
            "stub-model",
            checkout,
        )
        policy = PiAgentPolicy(bridge, session, GatewayClient(model, AGENT), LOOP, 30)
        return Played(bridge, policy, model, checkout)

    yield start
    for bridge in bridges:
        bridge.close()


def tools_of(request: dict[str, Any]) -> list[str]:
    return [tool["function"]["name"] for tool in request["tools"]]


def test_pis_turns_carry_its_own_prompt_and_tools_and_run_its_write_on_the_checkout(play: Any) -> None:
    played = play(
        [call("c1", "write", path="repo/b.py", content="b = 2\n"), call("c2", "list_dir", path="repo")],
        "Done.",
        [call("c3", "finish")],
    )
    listing = {
        "path": "repo",
        "entries": [{"name": "a.py", "type": "file"}, {"name": "b.py", "type": "file"}],
    }

    first = played.turn_of(results={"list_dir": listing})
    skipped = played.turn_of()
    last = played.turn_of()

    assert [(action.tool, action.call_id) for action in first or []] == [
        ("write", played_id(0, 0)),
        ("list_dir", played_id(0, 1)),
    ]
    assert (played.checkout / "repo" / "b.py").read_text() == "b = 2\n"
    assert skipped == [SKIP]
    assert [action.tool for action in last or []] == ["finish"]
    opening, after_tools, nudged = (request["messages"] for request in played.model.requests)
    assert "operating inside pi" in opening[0]["content"]
    assert opening[0]["content"].count(f"You are {AGENT}.") == 1
    assert opening[-1] == {"role": "user", "content": TASK}
    assert tools_of(played.model.requests[0]) == [
        "read",
        "bash",
        "edit",
        "write",
        "list_dir",
        "search_code",
        "finish",
    ]
    tool_messages = [message for message in after_tools if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == [played_id(0, 0), played_id(0, 1)]
    assert "Successfully wrote" in tool_messages[0]["content"]
    assert json.loads(tool_messages[1]["content"]) == listing
    assert nudged[-1] == {"role": "user", "content": NUDGE}
    assert played.transcript[0]["result"]["is_error"] is False


def test_a_call_pi_refuses_is_no_action_and_a_path_outside_the_checkout_is_never_read(play: Any) -> None:
    played = play(
        [call("c1", "read"), call("c2", "read", path="/etc/hosts"), call("c3", "nowhere")],
        [call("c4", "finish")],
    )

    first = played.turn_of()
    played.turn_of()

    assert [action.call_id for action in first or []] == [played_id(0, 1)]
    assert played.transcript[0]["result"]["is_error"] is True
    assert "escapes the checkout" in played.transcript[0]["result"]["content"]
    answered = {
        m["tool_call_id"]: m["content"] for m in played.model.requests[1]["messages"] if m["role"] == "tool"
    }
    assert set(answered) == {played_id(0, 0), played_id(0, 1), played_id(0, 2)}
    assert "path" in answered[played_id(0, 0)]
    assert "nowhere" in answered[played_id(0, 2)]


def test_a_call_id_the_model_reuses_is_a_new_call_each_time(play: Any) -> None:
    played = play(
        [
            call("c1", "write", path="repo/one.py", content="one\n"),
            call("c1", "write", path="repo/two.py", content="two\n"),
        ],
        [call("c1", "write", path="repo/three.py", content="three\n")],
        [call("c1", "finish")],
    )

    first, second = played.turn_of(), played.turn_of()
    played.turn_of()

    assert [(action.call_id, action.args["path"]) for action in (first or []) + (second or [])] == [
        (played_id(0, 0), "repo/one.py"),
        (played_id(0, 1), "repo/two.py"),
        (played_id(1, 0), "repo/three.py"),
    ]
    assert [(played.checkout / "repo" / name).read_text() for name in ("one.py", "two.py", "three.py")] == [
        "one\n",
        "two\n",
        "three\n",
    ]


def test_a_call_the_event_log_cannot_record_is_no_action_and_pi_is_told_why(play: Any) -> None:
    played = play(
        [call("c1", "write", path="repo/b.py", content="\ud800"), call("c2", "list_dir", path="repo")],
        [call("c3", "write", path="repo/c.py", content="\udfff")],
        [call("c4", "finish")],
    )

    first = played.turn_of(results={"list_dir": {"entries": []}})
    refused = played.turn_of()
    played.turn_of()

    assert [action.tool for action in first or []] == ["list_dir"]
    assert refused == [SKIP]
    assert not [name for name in ("b.py", "c.py") if (played.checkout / "repo" / name).exists()]
    told = {
        m["tool_call_id"]: m["content"] for m in played.model.requests[2]["messages"] if m["role"] == "tool"
    }
    assert UNRECORDABLE in told[played_id(0, 0)]
    assert UNRECORDABLE in told[played_id(1, 0)]


@pytest.mark.parametrize(
    ("ran", "said", "exit_code"),
    [
        (CommandResult(returncode=3, stdout="partial\n", stderr=""), "Command exited with code 3", 3),
        (
            CommandResult(returncode=None, stdout="partial\n", stderr=""),
            f"Command timed out after {StackSettings().shell_timeout_seconds:g} seconds",
            None,
        ),
        (SandboxError("the sandbox did not answer"), "the sandbox did not answer", None),
    ],
    ids=["exit code 3", "timed out", "sandbox broken"],
)
def test_a_command_that_failed_is_recorded_as_failed_with_what_pi_told_its_model(
    play: Any,
    ran: CommandResult | SandboxError,
    said: str,
    exit_code: int | None,
) -> None:
    played = play([call("c1", "bash", command="make")], [call("c2", "finish")], shell=sandbox(ran))

    played.turn_of()
    played.turn_of()

    result = played.transcript[0]["result"]
    assert (result["is_error"], result.get("exit_code")) == (True, exit_code)
    assert said in result["content"]
    told = [m["content"] for m in played.model.requests[1]["messages"] if m["role"] == "tool"]
    assert told == [result["content"]]


def test_the_inbox_reaches_pi_after_the_last_turns_results_and_before_its_next_model_call(play: Any) -> None:
    played = play([call("c1", "list_dir", path="repo")], [call("c2", "finish")])

    played.turn_of(results={"list_dir": {"entries": []}})
    played.turn_of(inbox=("Your access request r-1 for read on notes was approved by wen.",))

    messages = played.model.requests[1]["messages"]
    assert [message["role"] for message in messages[-2:]] == ["tool", "user"]
    assert messages[-1]["content"] == (
        "Message from iam (inbox): Your access request r-1 for read on notes was approved by wen."
    )


def test_a_failed_model_call_spends_the_turn_and_the_next_asks_again(play: Any) -> None:
    played = play(ProviderError("upstream 503"), [call("c1", "finish")])

    failed = played.turn_of()
    retried = played.turn_of()

    assert failed == [FAILED]
    assert [action.tool for action in retried or []] == ["finish"]
    assert played.model.requests[0]["messages"] == played.model.requests[1]["messages"]


def test_an_inbox_item_steered_while_pi_retries_a_failed_call_reaches_its_next_model_call(play: Any) -> None:
    played = play(ProviderError("upstream 503"), "Done.", [call("c1", "finish")])
    played.turn_of()
    played.turn_of(inbox=("Your access request r-1 for read on notes was approved by wen.",))

    resumed = played.turn_of()

    assert [action.tool for action in resumed or []] == ["finish"]
    assert "approved by wen" in played.model.requests[2]["messages"][-1]["content"]


def ask(url: str, body: object, key: str | None) -> tuple[int, Any]:
    headers = {"content-type": "application/json", **({"authorization": f"Bearer {key}"} if key else {})}
    request = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return error.code, None


def test_the_bridge_answers_an_agents_key_alone_and_runs_no_operation_outside_its_turn(play: Any) -> None:
    played = play([call("c1", "finish")])
    played.turn_of()
    url, key = f"{played.bridge.url}/{AGENT}", played.policy._session.key  # noqa: SLF001 - pi's own key

    assert ask(f"{url}/ops", {"id": 1, "op": "read", "path": "repo/a.py"}, None)[0] == 401
    assert ask(f"{url}/ops", {"id": 1, "op": "read", "path": "repo/a.py"}, "not-the-key")[0] == 401
    assert ask(f"{played.bridge.url}/agent-b/ops", {"id": 1, "op": "read"}, key)[0] == 401
    assert ask(
        f"{url}/ops",
        {"id": 1, "op": "read", "path": str(played.checkout / "repo" / "a.py")},
        key,
    ) == (
        200,
        {"status": "error", "error": "no tool call of yours is running"},
    )
    assert ask(f"{url}/nowhere", {}, key)[0] == 404


DETERMINISTIC = load_run_config("configs/aurora-efficiency.deterministic.yaml")
DETERMINISTIC_PI = load_run_config("configs/aurora-efficiency.deterministic-pi.yaml")


def honest(config: Any) -> Any:
    return dataclasses.replace(config, covert=dataclasses.replace(config.covert, enabled=False))


def actions_of(sealed: Path) -> list[tuple[str, str, object]]:
    events = [json.loads(line) for line in sealed.read_text().splitlines()]
    return [(e["actor_uid"], e["tool"], e["payload"]["args"]) for e in events if e["kind"] == "action"]


def test_an_episode_whose_setup_fails_stops_the_pis_it_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridges: list[PiBridge] = []
    start = PiBridge.start

    def start_one_pi(bridge: PiBridge, agent: str, *args: Any, **kwargs: Any) -> Any:
        bridges.append(bridge)
        if len(bridges) > 1:
            raise PiError(f"{agent}'s pi did not start")
        return start(bridge, agent, *args, **kwargs)

    monkeypatch.setattr(PiBridge, "start", start_one_pi)

    with pytest.raises(PiError, match="did not start"):
        play_graded_episode(DETERMINISTIC_PI, tmp_path)

    [started] = bridges[0]._sessions.values()  # noqa: SLF001 - the pi the episode started
    assert started.process.poll() is not None
    assert not started.directory.exists()
    with pytest.raises(ConnectionRefusedError):  # a bridge still serving would answer, if only with an error
        socket.create_connection(("127.0.0.1", urllib.parse.urlsplit(bridges[0].url).port), timeout=5)


@pytest.mark.parametrize("covert", [True, False], ids=["attack", "honest twin"])
def test_the_scripted_episode_plays_under_pi_as_it_does_natively(tmp_path: Path, covert: bool) -> None:
    native, pi = (config if covert else honest(config) for config in (DETERMINISTIC, DETERMINISTIC_PI))

    by_native = play_graded_episode(native, tmp_path / "native")
    by_pi = play_graded_episode(pi, tmp_path / "pi")

    renamed = {"write_file": "write", "read_file": "read", "bash": "bash"}
    assert actions_of(by_pi.sealed_path) == [
        (agent, renamed.get(tool, tool), args) for agent, tool, args in actions_of(by_native.sealed_path)
    ]
    assert {k: m.reached for k, m in by_pi.verdict.milestones.items()} == {
        k: m.reached for k, m in by_native.verdict.milestones.items()
    }
    assert by_pi.main_score.final_score == by_native.main_score.final_score
    events = [json.loads(line) for line in by_pi.sealed_path.read_text().splitlines()]
    agents = {agent.id for agent in pi.agents}
    turns = [e["payload"] for e in events if e["kind"] == "inference_call" and e["actor_uid"] in agents]
    assert turns
    assert all(call["credential_kind"] == "turn_token" and call["turn_id"] for call in turns)
