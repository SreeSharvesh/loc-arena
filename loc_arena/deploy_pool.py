"""The services a scenario's deployer deploys to, and how a stack run wires each to it.

The one live service with ``deploys:`` is the deployer. Each service it names runs under a supervisor
(``loc_arena.release_supervisor``) with a token of its own, which the deployer holds too: a supervisor takes a
release from the holder of its token alone.
"""

from __future__ import annotations

import secrets

from scenarios.loader import LiveService

from loc_arena.compose_document import ServiceSecret
from loc_arena.config import RunConfig

# The deployer's platform package: on every engine service's path, as IAM's is, never in a checkout.
PACKAGE = "meridian-deploy"
SUPERVISOR_MODULE = "loc_arena.release_supervisor"
RELEASE_TOKEN_FILE = "release_token"  # noqa: S105 - a supervisor's own token, as it finds it in its secrets


def find_deployer(config: RunConfig) -> LiveService | None:
    """The live service that deploys merged code, if the scenario declares one."""
    return next((service for service in config.live_services if service.deploys), None)


def list_deployed_services(config: RunConfig) -> list[str]:
    """Every service the deployer deploys to, in the order it lists them; none without a deployer."""
    deployer = find_deployer(config)
    return list(deployer.deploys) if deployer else []


def release_token_secret(service: str) -> str:
    """The compose secret holding the token of ``service``'s supervisor."""
    return f"release_token_{service.replace('-', '_')}"


def release_token_variable(service: str) -> str:
    """Where compose reads the token of ``service``'s supervisor from, on the host."""
    return f"LOC_ARENA_{release_token_secret(service).upper()}"


def declare_release_token_secrets(config: RunConfig) -> dict[str, dict[str, str]]:
    """Each supervisor's token as a compose secret, read from its host variable."""
    return {
        release_token_secret(service): {"environment": release_token_variable(service)}
        for service in list_deployed_services(config)
    }


def issue_release_tokens(config: RunConfig, token_bytes: int) -> dict[str, str]:
    """A new token for each supervisor of one play, by the host variable compose reads it from."""
    return {
        release_token_variable(service): secrets.token_urlsafe(token_bytes)
        for service in list_deployed_services(config)
    }


def run_module(config: RunConfig, service: str, module: str) -> str:
    """The module ``service``'s container runs: its supervisor if it is deployed to, else ``module``."""
    return SUPERVISOR_MODULE if service in list_deployed_services(config) else module


def mount_release_tokens(config: RunConfig, service: str) -> list[str | ServiceSecret]:
    """The tokens ``service`` mounts: its supervisor's own, or every supervisor's if it is the deployer."""
    deployed = list_deployed_services(config)
    if service in deployed:
        return [{"source": release_token_secret(service), "target": RELEASE_TOKEN_FILE}]
    deployer = find_deployer(config)
    return [release_token_secret(name) for name in deployed] if deployer and deployer.name == service else []
