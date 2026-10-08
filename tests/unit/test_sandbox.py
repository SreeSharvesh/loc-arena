"""The sandbox's command server: a command in a checkout for a token holder, nowhere else; a reset between.

The app, its runner and the processes it starts are real; the checkouts root is a temporary directory.
"""

from __future__ import annotations

import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.sandbox import create_sandbox_app
from loc_arena.settings import StackSettings
from pydantic import SecretStr

TOKEN = "a-sandbox-token-of-at-least-thirty-two-characters"
MARKER = "command-ran"


@pytest.fixture
def checkouts(tmp_path: Path) -> Path:
    (tmp_path / "checkouts" / "episode").mkdir(parents=True)
    return tmp_path / "checkouts"


def post_command(
    checkouts: Path,
    command: str,
    *,
    directory: Path | None = None,
    token: str | None = TOKEN,
    timeout_seconds: float = 10,
    environment: dict[str, str] | None = None,
    stack: StackSettings | None = None,
) -> tuple[int, Any]:
    """The status and the parsed body of a request to run ``command`` with bash."""
    stack = (stack or StackSettings()).model_copy(update={"checkouts_directory": checkouts})
    client = TestClient(create_sandbox_app(stack, SecretStr(TOKEN)))
    response = client.post(
        "/run",
        json={
            "argv": ["bash", "-c", command],
            "directory": str(directory or checkouts / "episode"),
            "timeout_seconds": timeout_seconds,
            "environment": environment or {},
        },
        headers={"authorization": f"Bearer {token}"} if token else {},
    )
    return response.status_code, response.json()


@pytest.mark.parametrize(
    ("stack", "command", "environment", "expected"),
    [
        (
            StackSettings(),
            "echo hello; echo oops >&2; exit 3",
            {},
            {"returncode": 3, "stdout": "hello\n", "stderr": "oops\n"},
        ),
        (
            StackSettings(),
            f"printenv {API_KEY_VARIABLE} || echo no key",
            {},
            {"returncode": 0, "stdout": "no key\n", "stderr": ""},
        ),
        (
            StackSettings(),
            "echo $GREETING",
            {"GREETING": "hi"},
            {"returncode": 0, "stdout": "hi\n", "stderr": ""},
        ),
        (
            StackSettings(command_output_limit_bytes=4),
            "printf abcdefgh; printf 12345678 >&2",
            {},
            {"returncode": 0, "stdout": "efgh", "stderr": "5678"},
        ),
    ],
    ids=["exit-code-and-output", "no-provider-key", "given-environment", "output-tails"],
)
def test_a_command_returns_its_exit_code_and_the_tails_of_its_output(
    checkouts: Path,
    monkeypatch: pytest.MonkeyPatch,
    stack: StackSettings,
    command: str,
    environment: dict[str, str],
    expected: dict[str, object],
) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, "sk-or-not-a-real-key")

    _status, result = post_command(checkouts, command, environment=environment, stack=stack)

    assert result == expected


def test_a_command_runs_in_the_requested_checkout_directory(checkouts: Path) -> None:
    _status, result = post_command(checkouts, "pwd")

    assert result["stdout"] == f"{(checkouts / 'episode').resolve()}\n"


@pytest.mark.parametrize("token", [None, f"not-{TOKEN}"], ids=["none", "wrong"])
def test_a_caller_without_the_token_is_refused_and_nothing_runs(checkouts: Path, token: str | None) -> None:
    status, _result = post_command(checkouts, f"touch {MARKER}", token=token)

    assert (status, (checkouts / "episode" / MARKER).exists()) == (HTTPStatus.UNAUTHORIZED, False)


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
    stack = StackSettings(checkouts_directory=checkouts, sandbox_scratch_directories=(scratch,))
    client = TestClient(create_sandbox_app(stack, SecretStr(TOKEN)))

    client.post(
        "/reset",
        json={"keep": str(checkouts / "episode" / "checkout")},
        headers={"authorization": f"Bearer {TOKEN}"},
    )

    left = (sorted(scratch.iterdir()), sorted(checkouts.iterdir()))
    assert (left, (checkouts / "episode" / "checkout" / "kept.py").read_text()) == (
        ([], [checkouts / "episode"]),
        "kept",
    )
