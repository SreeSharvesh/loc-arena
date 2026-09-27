from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.gateway.provider_key import PROVIDER_KEY_SECRET_NAME, load_provider_key

KEY = "sk-or-v1-canary-provider-key"


@pytest.fixture(autouse=True)
def no_key_in_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # a stray ./.env must never be read unless asked for


def test_reads_the_compose_secret_file(tmp_path: Path) -> None:
    (tmp_path / PROVIDER_KEY_SECRET_NAME).write_text(KEY + "\n")
    key = load_provider_key(secrets_directory=tmp_path)
    assert key is not None and key.get_secret_value() == KEY


def test_an_empty_or_missing_secret_is_no_key(tmp_path: Path) -> None:
    assert load_provider_key(secrets_directory=tmp_path) is None
    (tmp_path / PROVIDER_KEY_SECRET_NAME).write_text("")
    assert load_provider_key(secrets_directory=tmp_path) is None


def test_reads_the_dotenv_file_only_when_asked(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"OPENROUTER_API_KEY={KEY}\n")
    assert load_provider_key() is None
    key = load_provider_key(dotenv_path=tmp_path / ".env")
    assert key is not None and key.get_secret_value() == KEY


def test_the_environment_takes_priority(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / PROVIDER_KEY_SECRET_NAME).write_text("from-secret-file")
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=from-dotenv\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    key = load_provider_key(dotenv_path=tmp_path / ".env", secrets_directory=tmp_path)
    assert key is not None and key.get_secret_value() == KEY


def test_the_key_is_masked(tmp_path: Path) -> None:
    (tmp_path / PROVIDER_KEY_SECRET_NAME).write_text(KEY)
    key = load_provider_key(secrets_directory=tmp_path)
    assert KEY not in repr(key) and KEY not in str(key) and KEY not in f"{key}"
