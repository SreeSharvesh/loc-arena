"""Where the OpenRouter key comes from, loaded with pydantic-settings.

Each caller names its own sources (docs.pydantic.dev, pydantic-settings "Dotenv (.env) support" and
"Secrets"): the host harness reads the ``OPENROUTER_API_KEY`` environment variable or the repo's ``.env``;
gateway_core reads ``/run/secrets/openrouter_api_key``, the compose secret mounted into it alone. Environment
variables take priority over both. The key is a :class:`pydantic.SecretStr`, masked everywhere except
``get_secret_value()``.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# The compose secret's name: pydantic-settings reads a secrets directory by FIELD name, so the secret declared
# in configs/env.default.yaml must carry exactly this name.
PROVIDER_KEY_SECRET_NAME = "openrouter_api_key"
DOCKER_SECRETS_DIRECTORY = Path("/run/secrets")  # where compose mounts a service's secrets


class ProviderKeySettings(BaseSettings):
    """The provider key, from the environment plus whichever dotenv file or secrets directory is given."""

    model_config = SettingsConfigDict(extra="ignore")

    openrouter_api_key: SecretStr | None = None


def load_provider_key(
    *,
    dotenv_path: Path | None = None,
    secrets_directory: Path | None = None,
) -> SecretStr | None:
    """The provider key, or ``None`` when no source provides a non-empty one.

    ``None`` for a source disables it, so a caller never reads a file it did not ask for.
    """
    settings = ProviderKeySettings(_env_file=dotenv_path, _secrets_dir=secrets_directory)
    key = settings.openrouter_api_key
    return key if key is not None and key.get_secret_value() else None
