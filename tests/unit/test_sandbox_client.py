"""The episode's sandbox clients: every agent's token is read when they connect, and a failure says why."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.sandbox import SandboxClient, SandboxError, connect_sandboxes
from loc_arena.settings import GatewaySettings, LocArenaSettings
from loc_arena.task import SANDBOX_URL_VARIABLE
from pydantic import SecretStr, ValidationError
from sandbox_server.server import ServerSettings, create_sandbox_app
from sandbox_server.wire import CommandRequest

from tests.unit._jobsvc_support import serving

TOKEN = "a-sandbox-token-of-at-least-thirty-two-characters"


@pytest.mark.parametrize(
    ("serving_token", "failure"),
    [(None, FileNotFoundError), ("too-short", ValidationError)],
    ids=["missing", "too short"],
)
def test_connecting_fails_when_an_agents_token_is_missing_or_too_short(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    serving_token: str | None,
    failure: type[Exception],
) -> None:
    monkeypatch.setenv(SANDBOX_URL_VARIABLE, "http://sandbox-{agent}:8090")
    (tmp_path / "sandbox_token_agent_main").write_text(TOKEN)
    if serving_token is not None:
        (tmp_path / "sandbox_token_serving_agent").write_text(serving_token)
    settings = LocArenaSettings(gateway=GatewaySettings(secrets_dir=tmp_path))

    with pytest.raises(failure):
        connect_sandboxes(settings, ["agent-main", "serving-agent"])


def test_a_command_the_sandbox_cannot_start_fails_naming_why(tmp_path: Path) -> None:
    settings = ServerSettings(
        port=8090,
        checkouts_directory=tmp_path,
        scratch_directories=(),
        output_limit_bytes=None,
        secrets_dir=tmp_path,
        trusted_caller="127.0.0.1",
        command_timeout_limit_seconds=10,
    )
    missing = CommandRequest(argv=["true"], directory=tmp_path / "no-such-checkout", timeout_seconds=5)

    with serving(create_sandbox_app(settings, SecretStr(TOKEN))) as url:
        client = SandboxClient(
            url,
            SecretStr(TOKEN),
            response_grace_seconds=5,
            recovery_seconds=1,
            probe_interval_seconds=1,
        )
        with pytest.raises(SandboxError) as failed:
            client.run(missing)

    assert "FileNotFoundError" in str(failed.value)
