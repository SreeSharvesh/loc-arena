"""The stack's secrets load from the sources a caller names, masked; a container reads only /run/secrets."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.stack import stack_secrets
from loc_arena.stack.constants import CONTROL_KEY_SECRET_NAME, OPENROUTER_API_KEY_SECRET_NAME
from loc_arena.stack.stack_secrets import StackSecrets, load_container_secrets
from pydantic import SecretStr

SECRET_NAMES = [OPENROUTER_API_KEY_SECRET_NAME, CONTROL_KEY_SECRET_NAME]
SECRET_VALUE = "k" * 64
DOTENV_FILE_NAME = ".env"


@pytest.fixture(autouse=True)
def _no_secret_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SECRET_NAMES:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def container_secrets_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "run-secrets"
    directory.mkdir()
    monkeypatch.setattr(stack_secrets, "DOCKER_SECRETS_DIRECTORY", directory)
    monkeypatch.chdir(tmp_path)
    return directory


@pytest.mark.parametrize("secret_name", SECRET_NAMES)
def test_a_secret_file_is_read_under_its_secret_name(tmp_path: Path, secret_name: str) -> None:
    (tmp_path / secret_name).write_text(SECRET_VALUE)

    secrets = StackSecrets(_env_file=None, _secrets_dir=tmp_path)

    assert getattr(secrets, secret_name) == SecretStr(SECRET_VALUE)


def test_an_empty_secret_file_reads_as_none(tmp_path: Path) -> None:
    (tmp_path / CONTROL_KEY_SECRET_NAME).write_text("")

    secrets = StackSecrets(_env_file=None, _secrets_dir=tmp_path)

    assert secrets.control_key is None


def test_a_loaded_secret_is_masked_in_the_repr(tmp_path: Path) -> None:
    for name in SECRET_NAMES:
        (tmp_path / name).write_text(SECRET_VALUE)
    secrets = StackSecrets(_env_file=None, _secrets_dir=tmp_path)

    shown = repr(secrets)

    assert SECRET_VALUE not in shown


def test_the_environment_takes_priority_over_the_secrets_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / CONTROL_KEY_SECRET_NAME).write_text("from-file")
    monkeypatch.setenv(CONTROL_KEY_SECRET_NAME.upper(), "from-environment")

    secrets = StackSecrets(_env_file=None, _secrets_dir=tmp_path)

    assert secrets.control_key == SecretStr("from-environment")


def test_container_secrets_are_read_from_the_docker_secrets_directory(
    container_secrets_directory: Path,
) -> None:
    (container_secrets_directory / CONTROL_KEY_SECRET_NAME).write_text(SECRET_VALUE)

    secrets = load_container_secrets()

    assert secrets.control_key == SecretStr(SECRET_VALUE)


def test_container_secrets_never_come_from_a_dotenv_file(container_secrets_directory: Path) -> None:
    Path(DOTENV_FILE_NAME).write_text(f"{CONTROL_KEY_SECRET_NAME.upper()}={SECRET_VALUE}\n")

    secrets = load_container_secrets()

    assert secrets.control_key is None
