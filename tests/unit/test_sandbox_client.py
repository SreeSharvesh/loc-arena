"""The episode's sandbox clients: every agent's token is read when they connect, or the episode fails."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.sandbox import connect_sandboxes
from loc_arena.settings import GatewaySettings, LocArenaSettings
from loc_arena.task import SANDBOX_URL_VARIABLE
from pydantic import ValidationError

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
