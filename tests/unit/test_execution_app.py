from __future__ import annotations

import gzip
import os
import socketserver
import threading
import time
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path
from typing import get_args

import httpx
import pytest
from fastapi.testclient import TestClient
from loc_arena.execution.app import build_execution_app, create_execution_app
from loc_arena.execution.checkout import Checkout
from loc_arena.execution.client import ExecutionClient
from loc_arena.execution.workspace import SHELL_DISABLED_ERROR, Workspace
from loc_arena.stack.constants import (
    HEALTH_ROUTE,
    SANDBOX_AGENT_ID_ENVIRONMENT_VARIABLE,
    SETTINGS_ENVIRONMENT_VARIABLE,
    TOOL_CALLS_ROUTE,
    WORKSPACES_ROUTE,
)
from loc_arena.stack.contracts import BashResult, CodeToolCall, CodeToolName, CodeToolResult, ExecutionHealth
from loc_arena.stack.settings import ExecutionSettings, LocArenaSettings
from pydantic import JsonValue, ValidationError

from tests.unit._detached_processes import kill_recorded_processes, write_detaching_test

HANDLE = "0123456789abcdef"
OTHER_HANDLE = "fedcba9876543210"
REPOSITORY = "meridian-alpha"
SEEDED_MODULE = f"{REPOSITORY}/alpha/__init__.py"
SETTINGS = ExecutionSettings(
    max_read_characters=10,
    max_output_characters=50,
    max_matches=2,
    bash_timeout_seconds=1.0,
    run_tests_timeout_seconds=60.0,
)
PROMPT_SECONDS = 10.0  # far below the 600 s a detached `sleep 600` would hold a pipe open


def _write_repository(root: Path) -> None:
    package = root / REPOSITORY / "alpha"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1  # needle\n")
    tests = root / REPOSITORY / "tests"
    tests.mkdir()
    (tests / "test_alpha.py").write_text(
        "from alpha import VALUE\n\n\ndef test_value():\n    assert VALUE == 1\n",
    )


@pytest.fixture
def checkout_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))  # `bash -l` reads no profile of the machine's user
    root = tmp_path / "checkout"
    _write_repository(root)
    return root


@pytest.fixture
def workspace(checkout_root: Path) -> Workspace:
    return Workspace(Checkout(checkout_root, (REPOSITORY,)), SETTINGS, shell_enabled=True)


def _run(workspace: Workspace, tool: str, **arguments: JsonValue) -> dict[str, JsonValue]:
    return workspace.run(CodeToolCall.model_validate({"tool": tool, "arguments": arguments})).result


def _bash(workspace: Workspace, command: str) -> BashResult:
    return BashResult.model_validate(_run(workspace, "bash", command=command))


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until_gone(pid: int) -> bool:
    deadline = time.monotonic() + PROMPT_SECONDS
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def detached_pid_file(checkout_root: Path) -> Iterator[Path]:
    pid_file = checkout_root / "detached.pid"
    yield pid_file
    kill_recorded_processes(pid_file)


@pytest.fixture
def outside_file(checkout_root: Path) -> Path:
    outside = checkout_root.parent / "outside.txt"
    outside.write_text("secret")
    (checkout_root / "link.txt").symlink_to(outside)
    return outside


def _escaping_path(outside: Path, path_kind: str) -> str:
    return {"dot-dot": "../outside.txt", "absolute": str(outside), "symlink": "link.txt"}[path_kind]


@pytest.mark.parametrize("tool", ["read_file", "write_file", "list_dir"])
@pytest.mark.parametrize("path_kind", ["dot-dot", "absolute", "symlink"])
def test_a_path_resolving_outside_the_checkout_gives_an_error_result(
    workspace: Workspace,
    outside_file: Path,
    tool: str,
    path_kind: str,
) -> None:
    path = _escaping_path(outside_file, path_kind)

    result = _run(workspace, tool, path=path, content="overwritten")

    assert "escapes the checkout" in str(result["error"])


@pytest.mark.parametrize("path_kind", ["dot-dot", "absolute", "symlink"])
def test_a_write_to_a_path_resolving_outside_the_checkout_leaves_that_file_alone(
    workspace: Workspace,
    outside_file: Path,
    path_kind: str,
) -> None:
    path = _escaping_path(outside_file, path_kind)

    _run(workspace, "write_file", path=path, content="overwritten")

    assert outside_file.read_text() == "secret"


def test_a_symlink_loop_is_an_error_result(workspace: Workspace, checkout_root: Path) -> None:
    (checkout_root / "a").symlink_to(checkout_root / "b")
    (checkout_root / "b").symlink_to(checkout_root / "a")

    result = _run(workspace, "read_file", path="a")

    assert "bad args for read_file" in str(result["error"])


def test_a_search_skips_a_file_linked_from_outside(workspace: Workspace, checkout_root: Path) -> None:
    outside = checkout_root.parent / "outside.py"
    outside.write_text("needle outside\n")
    (checkout_root / REPOSITORY / "linked.py").symlink_to(outside)

    result = _run(workspace, "grep", pattern="needle")

    assert result["matches"] == [{"file": SEEDED_MODULE, "line": 1, "text": "VALUE = 1  # needle"}]


def test_a_read_returns_at_most_the_cap(workspace: Workspace, checkout_root: Path) -> None:
    (checkout_root / "long.txt").write_text("0123456789abcdef")

    result = _run(workspace, "read_file", path="long.txt")

    assert result == {"path": "long.txt", "content": "0123456789", "truncated": True}


def test_a_write_creates_its_parent_directories(
    workspace: Workspace,
    checkout_root: Path,
) -> None:
    _run(workspace, "write_file", path="new/note.txt", content="old old")

    assert (checkout_root / "new" / "note.txt").read_text() == "old old"


def test_an_edit_replaces_every_occurrence(workspace: Workspace, checkout_root: Path) -> None:
    (checkout_root / "note.txt").write_text("old old")

    _run(workspace, "edit_file", path="note.txt", old="old", new="new")

    assert (checkout_root / "note.txt").read_text() == "new new"


def test_a_listing_names_each_entry_with_its_type(workspace: Workspace) -> None:
    result = _run(workspace, "list_dir", path=REPOSITORY)

    assert result["entries"] == [{"name": "alpha", "type": "dir"}, {"name": "tests", "type": "dir"}]


def test_a_search_stops_at_the_match_cap(workspace: Workspace, checkout_root: Path) -> None:
    (checkout_root / REPOSITORY / "more.py").write_text("needle 1\nneedle 2\n")

    result = _run(workspace, "grep", pattern="needle")

    assert result == {  # SETTINGS.max_matches is 2: the third match, in more.py, is left out
        "matches": [
            {"file": SEEDED_MODULE, "line": 1, "text": "VALUE = 1  # needle"},
            {"file": f"{REPOSITORY}/more.py", "line": 1, "text": "needle 1"},
        ],
        "truncated": True,
    }


@pytest.mark.parametrize(
    ("tool", "arguments", "reason"),
    [
        ("write_file", {"content": "no path"}, "bad args for write_file"),
        ("search_code", {"pattern": "("}, "bad regex"),
    ],
)
def test_bad_arguments_give_an_error_result(
    workspace: Workspace,
    tool: str,
    arguments: dict[str, JsonValue],
    reason: str,
) -> None:
    result = _run(workspace, tool, **arguments)

    assert reason in str(result["error"])


@pytest.mark.parametrize("tool", get_args(CodeToolName))
def test_every_allowlisted_tool_answers_a_call_without_arguments(workspace: Workspace, tool: str) -> None:
    call = CodeToolCall.model_validate({"tool": tool, "arguments": {}})

    result = workspace.run(call)

    assert result.result  # an answer, an error or not: never an exception


def test_run_tests_reports_a_passing_suite(workspace: Workspace) -> None:
    result = _run(workspace, "run_tests", repo=REPOSITORY)

    assert result["passed"] is True
    assert "1 passed" in str(result["summary"])


def test_run_tests_refuses_an_unknown_repository(workspace: Workspace) -> None:
    result = _run(workspace, "run_tests", repo="meridian-missing")

    assert "unknown repo" in str(result["error"])


def test_run_tests_reports_a_suite_killed_at_its_timeout(checkout_root: Path) -> None:
    (checkout_root / REPOSITORY / "tests" / "test_slow.py").write_text(
        "import time\n\n\ndef test_slow():\n    time.sleep(30)\n",
    )
    settings = SETTINGS.model_copy(update={"run_tests_timeout_seconds": 1.0})
    workspace = Workspace(Checkout(checkout_root, (REPOSITORY,)), settings)

    result = _run(workspace, "run_tests", repo=REPOSITORY)

    assert (result["returncode"], result["summary"]) == (None, "timed out after 1 s")


def test_run_tests_returns_promptly_when_the_suite_detaches_a_process(
    workspace: Workspace,
    checkout_root: Path,
    detached_pid_file: Path,
) -> None:
    write_detaching_test(checkout_root / REPOSITORY, detached_pid_file)
    started = time.monotonic()

    _run(workspace, "run_tests", repo=REPOSITORY)

    assert time.monotonic() - started < PROMPT_SECONDS


def test_a_failing_benchmark_reports_the_end_of_its_stderr(workspace: Workspace) -> None:
    result = _run(workspace, "run_benchmark")  # the test checkout has no meridian_common to import

    assert result["error"] == "benchmark failed"
    assert "No module named 'meridian_common'" in str(result["stderr"])


def test_bash_returns_the_exit_code(workspace: Workspace) -> None:
    result = _bash(workspace, "exit 3")

    assert result.exit_code == 3


def test_bash_runs_in_the_checkout(workspace: Workspace) -> None:
    result = _bash(workspace, "ls")

    assert result.output == f"{REPOSITORY}\n"


def test_bash_output_includes_stderr_in_order(workspace: Workspace) -> None:
    result = _bash(workspace, "echo to-stdout; echo to-stderr >&2")

    assert result.output == "to-stdout\nto-stderr\n"


def test_bash_output_is_capped(workspace: Workspace) -> None:
    result = _bash(workspace, "head -c 100 /dev/zero | tr '\\0' x")

    assert result.output == "x" * SETTINGS.max_output_characters
    assert result.truncated


def test_a_bash_command_past_its_timeout_returns_promptly(workspace: Workspace) -> None:
    started = time.monotonic()

    _bash(workspace, "sleep 30")

    assert time.monotonic() - started < PROMPT_SECONDS


def test_a_bash_command_past_its_timeout_is_reported_timed_out_with_no_exit_code(
    workspace: Workspace,
) -> None:
    result = _bash(workspace, "sleep 30")

    assert (result.exit_code, result.timed_out) == (None, True)


def test_a_bash_timeout_kills_the_commands_background_children(
    workspace: Workspace,
    detached_pid_file: Path,
) -> None:
    _bash(workspace, f"sleep 30 & echo $! > {detached_pid_file.name}; wait")

    assert _wait_until_gone(int(detached_pid_file.read_text())), "a background child outlived the timeout"


def test_a_process_bash_detaches_does_not_hold_the_call(
    workspace: Workspace,
    detached_pid_file: Path,
) -> None:
    started = time.monotonic()

    _bash(workspace, f"sleep 600 & echo $! > {detached_pid_file.name}")

    assert time.monotonic() - started < PROMPT_SECONDS


def test_a_process_bash_detaches_keeps_running(workspace: Workspace, detached_pid_file: Path) -> None:
    _bash(workspace, f"sleep 600 & echo $! > {detached_pid_file.name}")

    assert _is_alive(int(detached_pid_file.read_text()))


@pytest.fixture
def shell_less_workspace(checkout_root: Path) -> Workspace:
    return Workspace(Checkout(checkout_root, (REPOSITORY,)), SETTINGS)


def test_bash_without_the_shell_answers_that_the_shell_runs_only_in_a_sandbox(
    shell_less_workspace: Workspace,
) -> None:
    result = _run(shell_less_workspace, "bash", command="true")

    assert result == {"error": SHELL_DISABLED_ERROR, "tool": "bash"}


def test_bash_without_the_shell_starts_no_process(
    shell_less_workspace: Workspace,
    checkout_root: Path,
) -> None:
    _run(shell_less_workspace, "bash", command="touch marker")

    assert not (checkout_root / "marker").exists()


@pytest.fixture
def company(tmp_path: Path) -> Path:
    root = tmp_path / "company"
    _write_repository(root)
    return root


@pytest.fixture
def served_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def served(served_checkout: Path, company: Path) -> Iterator[TestClient]:
    workspace = Workspace(Checkout(served_checkout, (REPOSITORY,)), SETTINGS, shell_enabled=True)
    with TestClient(create_execution_app(workspace, agent_id="agent-main", seed_source=company)) as client:
        yield client


@pytest.fixture
def opened(served: TestClient) -> TestClient:
    assert served.post(WORKSPACES_ROUTE, json={"handle": HANDLE}).status_code == HTTPStatus.CREATED
    return served


def _tool_call(client: TestClient, handle: str, tool: str, **arguments: JsonValue) -> httpx.Response:
    body = {"tool": tool, "arguments": arguments}
    return client.post(TOOL_CALLS_ROUTE.format(handle=handle), json=body)


def test_health_names_the_agent_served(served: TestClient) -> None:
    reply = served.get(HEALTH_ROUTE)

    assert ExecutionHealth.model_validate(reply.json()).agent_id == "agent-main"


def test_a_tool_call_before_the_workspace_opens_is_refused_with_404(served: TestClient) -> None:
    reply = _tool_call(served, HANDLE, "list_dir")

    assert reply.status_code == HTTPStatus.NOT_FOUND


def test_opening_the_workspace_seeds_the_checkout(served: TestClient, served_checkout: Path) -> None:
    served.post(WORKSPACES_ROUTE, json={"handle": HANDLE})

    assert (served_checkout / SEEDED_MODULE).read_text() == "VALUE = 1  # needle\n"


def test_reopening_the_workspace_keeps_the_agents_work(opened: TestClient, served_checkout: Path) -> None:
    (served_checkout / SEEDED_MODULE).write_text("edited")

    opened.post(WORKSPACES_ROUTE, json={"handle": HANDLE})

    assert (served_checkout / SEEDED_MODULE).read_text() == "edited"


def test_opening_the_workspace_for_another_episode_is_refused_with_409(opened: TestClient) -> None:
    reply = opened.post(WORKSPACES_ROUTE, json={"handle": OTHER_HANDLE})

    assert reply.status_code == HTTPStatus.CONFLICT


def test_a_tool_call_for_another_episode_is_refused_with_404(opened: TestClient) -> None:
    reply = _tool_call(opened, OTHER_HANDLE, "list_dir")

    assert reply.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(("handle", "tool"), [(HANDLE, "exec"), ("NOT-A-HANDLE", "list_dir")])
def test_an_unknown_tool_or_a_malformed_handle_is_refused_with_422(
    opened: TestClient,
    handle: str,
    tool: str,
) -> None:
    reply = _tool_call(opened, handle, tool)

    assert reply.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.fixture
def served_by_the_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    settings = LocArenaSettings(execution=ExecutionSettings(workspace_root=tmp_path))
    monkeypatch.setenv(SETTINGS_ENVIRONMENT_VARIABLE, settings.model_dump_json())
    monkeypatch.setenv(SANDBOX_AGENT_ID_ENVIRONMENT_VARIABLE, "serving-agent")
    monkeypatch.setenv("HOME", str(tmp_path))  # `bash -l` reads no profile of the machine's user
    with TestClient(build_execution_app()) as client:
        yield client


def test_the_factory_serves_the_agent_named_in_the_environment(served_by_the_factory: TestClient) -> None:
    reply = served_by_the_factory.get(HEALTH_ROUTE)

    assert ExecutionHealth.model_validate(reply.json()).agent_id == "serving-agent"


def test_the_factory_gives_the_sandbox_a_shell(served_by_the_factory: TestClient) -> None:
    served_by_the_factory.post(WORKSPACES_ROUTE, json={"handle": HANDLE})

    reply = _tool_call(served_by_the_factory, HANDLE, "bash", command="echo hi")

    assert BashResult.model_validate(CodeToolResult.model_validate(reply.json()).result).output == "hi\n"


def _client_of_app(served: TestClient, handle: str = HANDLE) -> ExecutionClient:
    return ExecutionClient(
        "http://sandbox",
        handle,
        SETTINGS,
        open_transport=lambda: httpx.ASGITransport(app=served.app),
    )


def test_the_client_runs_a_tool_in_the_opened_workspace(served: TestClient) -> None:
    client = _client_of_app(served)
    client.open_workspace()

    result = client.run(CodeToolCall(tool="list_dir", arguments={"path": REPOSITORY}))

    assert result.result["entries"] == [{"name": "alpha", "type": "dir"}, {"name": "tests", "type": "dir"}]


def test_the_client_raises_when_the_workspace_cannot_open(served: TestClient) -> None:
    _client_of_app(served, OTHER_HANDLE).open_workspace()

    with pytest.raises(httpx.HTTPStatusError):
        _client_of_app(served).open_workspace()


# A server the agent could run in the app's place: a canned reply, or one trickled a byte per tick.
DEADLINE_SECONDS = 1.0
TRICKLE_TICK_SECONDS = 0.05
TRICKLE_LIMIT_SECONDS = PROMPT_SECONDS
MAX_RESPONSE_BYTES = 1000
DEADLINE_SETTINGS = ExecutionSettings(
    bash_timeout_seconds=0.5,
    run_tests_timeout_seconds=0.5,
    run_benchmark_timeout_seconds=0.5,
    reply_timeout_seconds=DEADLINE_SECONDS,
    max_response_bytes=MAX_RESPONSE_BYTES,
)
VALID_REPLY = b'{"result": {"content": "ok"}}'
TRICKLED_REPLY_STARTS = {
    "headers": b"HTTP/1.1 200 OK\r\nX-Trickle: ",  # a header line that never ends
    "body": b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000000\r\n\r\n",
}
BASH_CALL = CodeToolCall(tool="bash", arguments={"command": "true"})


def _http_reply(body: bytes, status: HTTPStatus = HTTPStatus.OK, *, encoding: str = "identity") -> bytes:
    head = (
        f"HTTP/1.1 {status.value} {status.phrase}\r\n"
        f"Content-Encoding: {encoding}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + body


class TricklingServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self) -> None:
        """Listen on a free local port, answering ``VALID_REPLY`` at once until a test changes it."""
        super().__init__(("127.0.0.1", 0), TricklingHandler)
        self.mode = "answer"
        self.reply = _http_reply(VALID_REPLY)
        self.disconnected = threading.Event()  # a write failed: the client closed the connection
        self.stopping = threading.Event()

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host!s}:{port}"


class TricklingHandler(socketserver.StreamRequestHandler):
    server: TricklingServer

    def handle(self) -> None:
        content_length = 0
        while (line := self.rfile.readline()) not in {b"\r\n", b""}:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                content_length = int(value)
        self.rfile.read(content_length)
        if self.server.mode == "answer":
            self.wfile.write(self.server.reply)
            return
        gives_up_at = time.monotonic() + TRICKLE_LIMIT_SECONDS
        try:
            self.wfile.write(TRICKLED_REPLY_STARTS[self.server.mode])
            while not self.server.stopping.wait(TRICKLE_TICK_SECONDS) and time.monotonic() < gives_up_at:
                self.wfile.write(b"a" if self.server.mode == "headers" else b" ")
        except OSError:
            self.server.disconnected.set()


@pytest.fixture
def trickling_server() -> Iterator[TricklingServer]:
    server = TricklingServer()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.stopping.set()
        server.shutdown()
        server.server_close()


@pytest.fixture
def trickling_client(trickling_server: TricklingServer) -> ExecutionClient:
    return ExecutionClient(trickling_server.url, HANDLE, DEADLINE_SETTINGS)


# A small compressed body (well under MAX_RESPONSE_BYTES) that decodes far past it.
GZIP_BOMB = gzip.compress(b'{"result": {"content": "' + b"x" * 100_000 + b'"}}')
OVERSIZED_BODY = b'{"result": {"content": "' + b"x" * (2 * MAX_RESPONSE_BYTES) + b'"}}'


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        pytest.param(_http_reply(OVERSIZED_BODY), f"exceeds {MAX_RESPONSE_BYTES} bytes", id="oversized"),
        pytest.param(_http_reply(GZIP_BOMB, encoding="gzip"), "encoded", id="compressed"),
        pytest.param(_http_reply(b"not json"), "Invalid JSON", id="not-json"),
        pytest.param(_http_reply(b'{"result": {}, "injected": true}'), "Extra inputs", id="extra-field"),
        pytest.param(_http_reply(b"", HTTPStatus.INTERNAL_SERVER_ERROR), "500", id="server-error"),
    ],
)
def test_a_hostile_or_broken_reply_is_an_error_result_saying_why(
    trickling_server: TricklingServer,
    trickling_client: ExecutionClient,
    reply: bytes,
    reason: str,
) -> None:
    trickling_server.reply = reply

    result = trickling_client.run(BASH_CALL)

    assert reason in str(result.result["error"])


def test_the_error_result_of_a_broken_reply_names_the_tool(
    trickling_server: TricklingServer,
    trickling_client: ExecutionClient,
) -> None:
    trickling_server.reply = _http_reply(b"", HTTPStatus.INTERNAL_SERVER_ERROR)

    result = trickling_client.run(BASH_CALL)

    assert result.result["tool"] == BASH_CALL.tool


def test_a_reply_within_the_cap_is_returned(trickling_client: ExecutionClient) -> None:
    result = trickling_client.run(BASH_CALL)

    assert result.result == {"content": "ok"}


@pytest.mark.parametrize("mode", ["headers", "body"])
def test_a_reply_trickled_past_the_deadline_is_an_error_result_at_the_deadline(
    trickling_server: TricklingServer,
    trickling_client: ExecutionClient,
    mode: str,
) -> None:
    trickling_server.mode = mode
    started = time.monotonic()

    result = trickling_client.run(BASH_CALL)

    elapsed = time.monotonic() - started
    assert "no complete reply within the 1 s deadline" in str(result.result["error"])
    assert DEADLINE_SECONDS <= elapsed < DEADLINE_SECONDS + PROMPT_SECONDS / 2


@pytest.mark.parametrize("mode", ["headers", "body"])
def test_a_reply_trickled_past_the_deadline_has_its_connection_closed(
    trickling_server: TricklingServer,
    trickling_client: ExecutionClient,
    mode: str,
) -> None:
    trickling_server.mode = mode

    trickling_client.run(BASH_CALL)

    assert trickling_server.disconnected.wait(PROMPT_SECONDS / 2), "the client left the connection open"


def test_a_call_cut_at_the_deadline_leaves_no_thread_behind(trickling_server: TricklingServer) -> None:
    trickling_server.mode = "headers"
    port = trickling_server.server_address[1]
    # localhost resolves in the event loop's executor, a thread of its own.
    client = ExecutionClient(f"http://localhost:{port}", HANDLE, DEADLINE_SETTINGS)

    client.run(BASH_CALL)

    assert not [thread for thread in threading.enumerate() if thread.name.startswith("asyncio")]


def test_a_workspace_that_opens_past_the_deadline_raises_an_httpx_error(
    trickling_server: TricklingServer,
    trickling_client: ExecutionClient,
) -> None:
    trickling_server.mode = "body"

    with pytest.raises(httpx.TimeoutException, match="deadline"):
        trickling_client.open_workspace()


def test_the_reply_timeout_must_exceed_every_tool_timeout() -> None:
    with pytest.raises(ValidationError, match="reply_timeout_seconds"):
        ExecutionSettings(bash_timeout_seconds=400.0)
