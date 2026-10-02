from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.gateway.core_control_client import CoreControlClient
from loc_arena.stack import stack_secrets
from loc_arena.stack.constants import (
    CONTROL_KEY_SECRET_NAME,
    OPENROUTER_API_KEY_SECRET_NAME,
    SETTINGS_ENVIRONMENT_VARIABLE,
)
from loc_arena.stack.contracts import EpisodeOpened
from loc_arena.stack.settings import LocArenaSettings

from tests.unit._gateway_stack import OPENING, GatewayStack, serve_gateway


@pytest.fixture
def stack(tmp_path: Path) -> GatewayStack:
    return serve_gateway(tmp_path)


@pytest.fixture
def opened(stack: GatewayStack) -> EpisodeOpened:
    return stack.open_episode()


@pytest.fixture
def control(stack: GatewayStack) -> CoreControlClient:
    return CoreControlClient.open_episode(stack.control, OPENING)


@pytest.fixture
def secrets_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "run-secrets"
    directory.mkdir()
    for name in (OPENROUTER_API_KEY_SECRET_NAME, CONTROL_KEY_SECRET_NAME):
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv(SETTINGS_ENVIRONMENT_VARIABLE, LocArenaSettings().model_dump_json())
    monkeypatch.setattr(stack_secrets, "DOCKER_SECRETS_DIRECTORY", directory)
    return directory
