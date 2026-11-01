"""pi as an agent's harness: a real pi, at the version the Dockerfile pins, and a stub for its model.

The episode plays the agent's turns as ``Agent.run_turn`` does: the policy's actions, pi's own tools run
through the bridge, an arena tool's result written into the transcript, the IAM inbox's items before a turn.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import GenerateResponse, ProviderError
from loc_arena.scaffold.agent import FAILED, SKIP, Transcript
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.pi_harness import NUDGE, TASK, PiAgentPolicy, PiBridge
from loc_arena.scaffold.tool_specs import agent_tool_specs
from loc_arena.scaffold.tools import AgentAction
from loc_arena.settings import PiSettings, StackSettings

from tests.integration._live_support import play_graded_episode

PI = shutil.which("pi")
PI_VERSION = "0.99.2"
NEEDS_PI = pytest.mark.skipif(
    PI is None
    or subprocess.run([PI, "--version"], capture_output=True, text=True).stdout.strip() != PI_VERSION,
    reason=f"pi {PI_VERSION} is not on PATH",
)
pytestmark = NEEDS_PI
LOOP = load_run_config("configs/aurora-efficiency.deterministic.yaml").agent_loop
AGENT = "agent-a"


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

    def start(*replies: list[dict[str, Any]] | str | Exception) -> Played:
        checkout = tmp_path / "checkout"
        (checkout / "repo").mkdir(parents=True)
        (checkout / "repo" / "a.py").write_text("a = 1\n")
        code = CodeServices(checkout=checkout, repos=["repo"], stack=StackSettings())
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
            shell=False,
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

    assert [(action.tool, action.call_id) for action in first or []] == [("write", "c1"), ("list_dir", "c2")]
    assert (played.checkout / "repo" / "b.py").read_text() == "b = 2\n"
    assert skipped == [SKIP]
    assert [action.tool for action in last or []] == ["finish"]
    opening, after_tools, nudged = (request["messages"] for request in played.model.requests)
    assert "operating inside pi" in opening[0]["content"]
    assert opening[0]["content"].count(f"You are {AGENT}.") == 1
    assert opening[-1] == {"role": "user", "content": TASK}
    assert tools_of(played.model.requests[0]) == [
        "read",
        "edit",
        "write",
        "list_dir",
        "search_code",
        "finish",
    ]
    tool_messages = [message for message in after_tools if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == ["c1", "c2"]
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

    assert [action.call_id for action in first or []] == ["c2"]
    assert played.transcript[0]["result"]["is_error"] is True
    assert "escapes the checkout" in played.transcript[0]["result"]["content"]
    answered = {
        m["tool_call_id"]: m["content"] for m in played.model.requests[1]["messages"] if m["role"] == "tool"
    }
    assert set(answered) == {"c1", "c2", "c3"}
    assert "path" in answered["c1"]
    assert "nowhere" in answered["c3"]


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
