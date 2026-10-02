"""The stack's two secrets, loaded with pydantic-settings from the sources each caller names."""

from __future__ import annotations

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from loc_arena.stack.constants import DOCKER_SECRETS_DIRECTORY


class StackSecrets(BaseSettings):
    """The provider key (gateway_core only) and the control key (core, edge and runner)."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    openrouter_api_key: SecretStr | None = None
    control_key: SecretStr | None = None

    @field_validator("openrouter_api_key", "control_key")
    @classmethod
    def _drop_empty(cls, value: SecretStr | None) -> SecretStr | None:
        return value if value is not None and value.get_secret_value() else None


def load_container_secrets() -> StackSecrets:
    """The secrets compose mounted into this container, plus the environment (never a dotenv file)."""
    return StackSecrets(_env_file=None, _secrets_dir=DOCKER_SECRETS_DIRECTORY)
