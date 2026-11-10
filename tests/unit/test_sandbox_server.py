"""The sandbox's command server: a command in a checkout for its trusted caller with the token; resets.

The app, its runner and the processes it starts are real; the checkouts root is a temporary directory, and the
trusted caller is this host's loopback address.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sandbox_server.command import API_KEY_VARIABLE
from sandbox_server.server import ServerSettings, create_sandbox_app

TOKEN = "a-sandbox-token-of-at-least-thirty-two-characters"
MARKER = "command-ran"
OUTPUT_LIMIT_BYTES = 10_000
TRUSTED_CALLER = "127.0.0.1"
OTHER_CALLER = "192.0.2.7"  # an address reserved for documentation: no name resolves to it here
TIMEOUT_LIMIT_SECONDS = 30.0
LATE_SECONDS = 0.3  # how long after it is asked for a late kill of leftovers lands, as on a busy server


@pytest.fixture
def checkouts(tmp_path: Path) -> Path:
    (tmp_path / "checkouts" / "episode").mkdir(parents=True)
    return tmp_path / "checkouts"


@pytest.fixture
def pids(tmp_path: Path) -> Path:
    """Where a command records its process id while it runs, for ``end_recorded`` to end it."""
    (tmp_path / "pids").mkdir()
    return tmp_path / "pids"


def serve(
    checkouts: Path,
    output_limit_bytes: int = OUTPUT_LIMIT_BYTES,
    scratch: Path | None = None,
    caller: str = TRUSTED_CALLER,
    end_leftovers: Callable[[], None] = lambda: None,
) -> TestClient:
    """The command server over ``checkouts``, as ``caller`` reaches it, with ``scratch`` if given."""
    settings = ServerSettings(
        port=8090,
        checkouts_directory=checkouts,
        scratch_directories=(scratch,) if scratch else (),
        output_limit_bytes=output_limit_bytes,
        secrets_dir=checkouts.parent / "secrets",
        trusted_caller=TRUSTED_CALLER,
        command_timeout_limit_seconds=TIMEOUT_LIMIT_SECONDS,
    )
    app = create_sandbox_app(settings, SecretStr(TOKEN), end_leftovers=end_leftovers)
    return TestClient(app, client=(caller, 50000))


def post_command(
    checkouts: Path,
    command: str,
    *,
    directory: Path | None = None,
    token: str | None = TOKEN,
    timeout_seconds: float = 10,
    environment: dict[str, str] | None = None,
    stdin: str = "",
    output_limit_bytes: int = OUTPUT_LIMIT_BYTES,
    caller: str = TRUSTED_CALLER,
    server: TestClient | None = None,
) -> tuple[int, Any]:
    """The status and the parsed body of a request from ``caller`` to run ``command`` with bash.

    It goes to ``server`` if given, else to a new one.
    """
    response = (server or serve(checkouts, output_limit_bytes, caller=caller)).post(
        "/run",
        json={
            "argv": ["bash", "-c", command],
            "directory": str(directory or checkouts / "episode"),
            "timeout_seconds": timeout_seconds,
            "environment": environment or {},
            "stdin": stdin,
        },
        headers={"authorization": f"Bearer {token}"} if token else {},
    )
    return response.status_code, response.json()


def post_reset(server: TestClient, keep: Path) -> int:
    """The status of a request to ``server`` to reset its sandbox, keeping ``keep``."""
    reply = server.post("/reset", json={"keep": str(keep)}, headers={"authorization": f"Bearer {TOKEN}"})
    return reply.status_code


def end_recorded(pids: Path, *, late_seconds: float = 0, missed: int = 0) -> Callable[[], None]:
    """What ends every command recorded in ``pids``, as the container's kill ends every process.

    Each kill lands ``late_seconds`` after it is asked for, and the first ``missed`` kills end nothing.
    """
    misses = iter(range(missed))

    def kill_commands() -> None:
        time.sleep(late_seconds)
        if next(misses, None) is not None:
            return
        for recorded in pids.iterdir():
            with contextlib.suppress(ProcessLookupError):  # it ended meanwhile
                os.killpg(int(recorded.name), signal.SIGKILL)
            recorded.unlink(missing_ok=True)

    return kill_commands


def sleep_recorded(server: TestClient, checkouts: Path, pids: Path, seconds: float = 10) -> Any:
    """What ``server`` answers to a command that sleeps ``seconds``, recorded in ``pids`` while it runs."""
    return post_command(checkouts, f"touch {pids}/$$; sleep {seconds}; rm {pids}/$$", server=server)[1]


def start_recorded(server: TestClient, checkouts: Path, pids: Path) -> concurrent.futures.Future[Any]:
    """A command sleeping on ``server``, handed back once it runs: its answer comes when it ends."""
    running = concurrent.futures.ThreadPoolExecutor(1).submit(sleep_recorded, server, checkouts, pids)
    while not (any(pids.iterdir()) or running.done()):
        time.sleep(0.01)
    return running


@pytest.mark.parametrize(
    ("output_limit_bytes", "command", "environment", "expected"),
    [
        (
            OUTPUT_LIMIT_BYTES,
            "echo hello; echo oops >&2; exit 3",
            {},
            {"returncode": 3, "stdout": "hello\n", "stderr": "oops\n"},
        ),
        (
            OUTPUT_LIMIT_BYTES,
            f"printenv {API_KEY_VARIABLE} || echo no key",
            {},
            {"returncode": 0, "stdout": "no key\n", "stderr": ""},
        ),
        (
            OUTPUT_LIMIT_BYTES,
            f"printenv {API_KEY_VARIABLE} || echo no key",
            {API_KEY_VARIABLE: "sk-or-sent-by-the-caller"},
            {"returncode": 0, "stdout": "no key\n", "stderr": ""},
        ),
        (
            OUTPUT_LIMIT_BYTES,
            "echo $GREETING",
            {"GREETING": "hi"},
            {"returncode": 0, "stdout": "hi\n", "stderr": ""},
        ),
        (
            4,
            "printf abcdefgh; printf 12345678 >&2",
            {},
            {"returncode": 0, "stdout": "efgh", "stderr": "5678"},
        ),
    ],
    ids=[
        "exit-code-and-output",
        "no-provider-key",
        "no-provider-key-sent",
        "given-environment",
        "output-tails",
    ],
)
def test_a_command_returns_its_exit_code_and_the_tails_of_its_output(
    checkouts: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_limit_bytes: int,
    command: str,
    environment: dict[str, str],
    expected: dict[str, object],
) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "sk-or-not-a-real-key")

    _status, result = post_command(
        checkouts,
        command,
        environment=environment,
        output_limit_bytes=output_limit_bytes,
    )

    assert result == expected


@pytest.mark.parametrize("stdin", ["a checkpoint\n", ""], ids=["given", "none"])
def test_a_command_reads_the_stdin_it_is_given_or_an_empty_one(checkouts: Path, stdin: str) -> None:
    _status, result = post_command(checkouts, "cat", stdin=stdin, timeout_seconds=5)

    assert result == {"returncode": 0, "stdout": stdin, "stderr": ""}


def test_a_command_runs_in_the_requested_checkout_directory(checkouts: Path) -> None:
    _status, result = post_command(checkouts, "pwd")

    assert result["stdout"] == f"{(checkouts / 'episode').resolve()}\n"


@pytest.mark.parametrize("token", [None, f"not-{TOKEN}"], ids=["none", "wrong"])
def test_a_caller_without_the_token_is_refused_and_nothing_runs(checkouts: Path, token: str | None) -> None:
    status, _result = post_command(checkouts, f"touch {MARKER}", token=token)

    assert (status, (checkouts / "episode" / MARKER).exists()) == (HTTPStatus.UNAUTHORIZED, False)


def test_a_caller_other_than_the_trusted_one_is_refused_even_with_the_token_and_nothing_runs(
    checkouts: Path,
) -> None:
    status, _result = post_command(checkouts, f"touch {MARKER}", caller=OTHER_CALLER)

    assert (status, (checkouts / "episode" / MARKER).exists()) == (HTTPStatus.FORBIDDEN, False)


def test_a_command_with_a_timeout_over_the_limit_is_refused_and_nothing_runs(checkouts: Path) -> None:
    status, _result = post_command(checkouts, f"touch {MARKER}", timeout_seconds=TIMEOUT_LIMIT_SECONDS + 1)

    assert (status, (checkouts / "episode" / MARKER).exists()) == (HTTPStatus.UNPROCESSABLE_ENTITY, False)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_the_command_server_serves_no_framework_page(checkouts: Path, path: str) -> None:
    server = serve(checkouts)

    reply = server.get(path)

    assert reply.status_code == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("escape", ["outside", "dot-dot", "symlink"])
def test_a_directory_outside_the_checkouts_is_refused_and_nothing_runs(
    tmp_path: Path,
    checkouts: Path,
    escape: str,
) -> None:
    (checkouts / "link").symlink_to(tmp_path)
    directory = {"outside": tmp_path, "dot-dot": checkouts / "..", "symlink": checkouts / "link"}[escape]

    status, _result = post_command(checkouts, f"touch {MARKER}", directory=directory)

    assert (status, (tmp_path / MARKER).exists()) == (HTTPStatus.FORBIDDEN, False)


def test_a_timeout_kills_the_commands_whole_session(checkouts: Path) -> None:
    _status, result = post_command(checkouts, f"(sleep 1; touch {MARKER}) & sleep 5", timeout_seconds=0.2)
    time.sleep(1.5)  # past the moment a surviving child would have left its marker

    assert (result, (checkouts / "episode" / MARKER).exists()) == (
        {"returncode": None, "stdout": "", "stderr": ""},
        False,
    )


def test_a_reset_clears_the_scratch_directories_and_every_other_checkout_entry_but_keeps_the_new_checkout(
    tmp_path: Path,
    checkouts: Path,
) -> None:
    scratch = tmp_path / "scratch"
    (scratch / ".local").mkdir(parents=True)
    (scratch / "planted.py").write_text("planted")
    (checkouts / "stray").mkdir()
    (checkouts / "pytest.ini").write_text("[pytest]")
    (checkouts / "episode" / "checkout").mkdir()
    (checkouts / "episode" / "checkout" / "kept.py").write_text("kept")
    server = serve(checkouts, scratch=scratch)

    post_reset(server, checkouts / "episode" / "checkout")

    left = (sorted(scratch.iterdir()), sorted(checkouts.iterdir()))
    assert (left, (checkouts / "episode" / "checkout" / "kept.py").read_text()) == (
        ([], [checkouts / "episode"]),
        "kept",
    )


def test_a_reset_that_would_keep_the_whole_checkouts_directory_is_refused(checkouts: Path) -> None:
    (checkouts / "stray").mkdir()

    status = post_reset(serve(checkouts), checkouts)

    assert (status, (checkouts / "stray").exists()) == (HTTPStatus.FORBIDDEN, True)


def test_a_command_run_right_after_a_reset_outlives_the_end_of_the_command_the_reset_ended(
    checkouts: Path,
    pids: Path,
) -> None:
    """A reset ends a running command, whose handler then ends leftovers too: late, as each kill is here."""
    server = serve(checkouts, end_leftovers=end_recorded(pids, late_seconds=LATE_SECONDS))
    start_recorded(server, checkouts, pids)
    post_reset(server, checkouts / "episode")

    result = sleep_recorded(server, checkouts, pids, 2 * LATE_SECONDS)

    assert result["returncode"] == 0


def test_a_command_run_right_after_a_reset_outlives_the_end_of_a_command_sent_while_the_reset_waited(
    checkouts: Path,
    pids: Path,
) -> None:
    server = serve(checkouts, end_leftovers=end_recorded(pids, late_seconds=LATE_SECONDS))
    start_recorded(server, checkouts, pids)
    resetting = concurrent.futures.ThreadPoolExecutor(1).submit(post_reset, server, checkouts / "episode")
    while any(pids.iterdir()):  # until the reset has ended that command, and waits for its handler
        time.sleep(0.01)
    start_recorded(server, checkouts, pids)
    resetting.result()

    result = sleep_recorded(server, checkouts, pids, 2 * LATE_SECONDS)

    assert result["returncode"] == 0


def test_a_reset_ends_a_command_its_first_kill_missed(checkouts: Path, pids: Path) -> None:
    """A kill misses a command whose process has not started yet, and the reset waits for that command too."""
    server = serve(checkouts, end_leftovers=end_recorded(pids, missed=1))
    running = start_recorded(server, checkouts, pids)

    post_reset(server, checkouts / "episode")

    assert running.result()["returncode"] == -signal.SIGKILL


def test_the_command_server_loads_nothing_of_the_harness() -> None:
    code = (
        "import sys, json, sandbox_server, sandbox_server.server;"
        "print(json.dumps([name for name in sys.modules if name.startswith(('loc_arena', 'scenarios'))]))"
    )

    loaded = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)

    assert json.loads(loaded.stdout) == []
