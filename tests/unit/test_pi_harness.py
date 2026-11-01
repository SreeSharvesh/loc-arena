"""The pi harness's parts that need no pi: the agent's tools, scripted moves, pi's requests and replies."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from loc_arena.gateway.core import GenerateResponse, Move
from loc_arena.sandbox import SandboxError
from loc_arena.scaffold.code_services import CodeServices, SkillsFolder
from loc_arena.scaffold.pi_harness import arena_tools, as_pi_move, read_chat_request, stream_reply
from loc_arena.scaffold.tool_specs import agent_tool_specs
from loc_arena.settings import StackSettings

SERVED = {"type": "function", "function": {"name": "open_pr", "description": "Open a PR.", "parameters": {}}}


def names(specs: list[dict]) -> list[str]:
    return [spec["function"]["name"] for spec in specs]


def test_an_agent_keeps_the_arena_tools_pi_has_no_tool_for() -> None:
    kept = arena_tools(agent_tool_specs(shell=True, served=[SERVED]))

    assert names(kept) == ["list_dir", "search_code", "open_pr", "finish"]


@pytest.mark.parametrize(
    ("move", "played"),
    [
        (
            Move(tool="write_file", args={"path": "a.py", "content": "x"}),
            Move(tool="write", args={"path": "a.py", "content": "x"}),
        ),
        (Move(tool="read_file", args={"path": "a.py"}), Move(tool="read", args={"path": "a.py"})),
        (
            Move(tool="edit_file", args={"path": "a.py", "old": "x", "new": "y"}),
            Move(tool="edit", args={"path": "a.py", "edits": [{"oldText": "x", "newText": "y"}]}),
        ),
        (Move(tool="bash", args={"command": "ls"}), Move(tool="bash", args={"command": "ls"})),
        (Move(tool="open_pr", args={"repo": "r"}), Move(tool="open_pr", args={"repo": "r"})),
    ],
    ids=["write", "read", "edit", "bash", "a service's tool"],
)
def test_a_scripted_move_of_a_tool_pi_replaces_is_played_as_pis(move: Move, played: Move) -> None:
    assert as_pi_move(move) == played


def test_pis_request_reaches_the_gateway_with_every_content_as_text() -> None:
    call = {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
    body = {
        "model": "m",
        "messages": [
            {"role": "developer", "content": "the prompt"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "text", "text": "b", "cache_control": {}},
                ],
            },
            {"role": "assistant", "content": None, "tool_calls": [call], "reasoning_content": "hidden"},
            {"role": "tool", "tool_call_id": "c1", "content": [{"type": "text", "text": "read it"}]},
        ],
        "tools": [SERVED],
    }

    messages, tools = read_chat_request(body)

    assert messages == [
        {"role": "system", "content": "the prompt"},
        {"role": "user", "content": "ab"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "content": "read it", "tool_call_id": "c1"},
    ]
    assert tools == [SERVED]


def test_a_reply_streams_its_text_tool_calls_and_usage_as_chunks_pi_reads() -> None:
    calls = [
        {"id": "c1", "type": "function", "function": {"name": "write", "arguments": '{"path": "a"}'}},
        {"id": "c2", "type": "function", "function": {"name": "open_pr", "arguments": {"repo": "r"}}},
    ]
    reply = GenerateResponse(
        text="ok",
        turn_id="a:t0",
        batch_id=None,
        seq=7,
        tool_calls=calls,
        prompt_tokens=3,
        completion_tokens=2,
        cached_tokens=1,
    )

    stream = stream_reply(reply, "the-model").decode()

    events = [line.removeprefix("data: ") for line in stream.split("\n\n") if line]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert {chunk["id"] for chunk in chunks} == {"arena-7"}
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
    assert deltas[0] == {"role": "assistant", "content": "ok"}
    streamed = [delta["tool_calls"][0] for delta in deltas[1:3]]
    assert [
        (call["index"], call["id"], call["function"]["name"], call["function"]["arguments"])
        for call in streamed
    ] == [
        (0, "c1", "write", '{"path": "a"}'),
        (1, "c2", "open_pr", '{"repo": "r"}'),
    ]
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 1},
    }


def test_a_reply_calling_no_tool_stops() -> None:
    reply = GenerateResponse(text="done", turn_id=None, batch_id=None, seq=1)

    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in stream_reply(reply, "m").decode().split("\n\n")[:-2]
    ]

    assert [chunk["choices"][0]["finish_reason"] for chunk in chunks] == [None, "stop"]


@pytest.fixture
def code(tmp_path: Path) -> CodeServices:
    checkout, skills = tmp_path / "checkout", tmp_path / "skills"
    (checkout / "repo").mkdir(parents=True)
    (checkout / "repo" / "a.py").write_text("a = 1\n")
    (skills / "profile").mkdir(parents=True)
    (skills / "profile" / "SKILL.md").write_text("# profile\n")
    (tmp_path / "secret.txt").write_text("not the agent's")
    return CodeServices(
        checkout=checkout,
        repos=["repo"],
        stack=StackSettings(),
        skills=SkillsFolder(Path("/skills"), skills),
    )


def test_pis_operations_act_on_the_checkout(code: CodeServices, tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    code.make_directory(str(checkout / "repo" / "new"))
    code.write_text(str(checkout / "repo" / "new" / "b.py"), "b = 2\n")
    code.check_readable(str(checkout / "repo" / "a.py"))

    assert code.read_bytes(str(checkout / "repo" / "new" / "b.py")) == b"b = 2\n"
    assert code.read_bytes("/skills/profile/SKILL.md") == b"# profile\n"
    with pytest.raises(FileNotFoundError):
        code.check_readable(str(checkout / "repo" / "missing.py"))


@pytest.mark.parametrize("operation", ["read_bytes", "check_readable", "write_text", "make_directory"])
def test_pis_operations_refuse_a_path_outside_the_checkout(
    code: CodeServices,
    tmp_path: Path,
    operation: str,
) -> None:
    outside = str(tmp_path / "checkout" / ".." / "secret.txt")
    arguments = [outside, "overwritten"] if operation == "write_text" else [outside]

    with pytest.raises(ValueError, match="escapes the checkout"):
        getattr(code, operation)(*arguments)

    assert (tmp_path / "secret.txt").read_text() == "not the agent's"


def test_pis_skills_folder_is_read_only(code: CodeServices) -> None:
    with pytest.raises(ValueError, match="escapes the checkout"):
        code.write_text("/skills/profile/SKILL.md", "rewritten")


def test_a_command_runs_only_in_a_sandbox(code: CodeServices, tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="has no sandbox"):
        code.run_command("agent-main", "ls", str(tmp_path / "checkout"), None)
