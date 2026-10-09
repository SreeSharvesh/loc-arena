"""The episode's client of the sandbox: the container where agent-written code runs in a stack run.

The sandbox's command server (``sandbox_server``) runs one command at a caller's request, in a directory under
``settings.stack.checkouts_directory``, for a caller holding the sandbox token (a compose secret mounted into
the episode and the sandbox only). In the episode container the agents' bash, run_tests and run_benchmark go
to the sandbox; elsewhere bash is refused and the other two run in this process.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx2
import tenacity
from pydantic import BaseModel, SecretStr
from sandbox_server.server import SandboxSecrets, ServerSettings
from sandbox_server.wire import RESET_PATH, RUN_PATH, CommandRequest, CommandResult, ResetRequest

from loc_arena.settings import LocArenaSettings
from loc_arena.task import sandbox_url

SANDBOX_SERVICE = "sandbox"  # the sandbox's compose service, and its host name on agent-net
TOKEN_SECRET_NAME = "sandbox_token"  # the compose secret: a file in settings.gateway.secrets_dir
TOKEN_VARIABLE = "LOC_ARENA_SANDBOX_TOKEN"  # where compose reads the secret from, on the host


class SandboxError(RuntimeError):
    """The sandbox did not answer, or refused: the tool that asked gets an error result."""


@dataclass(frozen=True)
class SandboxClient:
    """The episode's way into the sandbox's command server."""

    url: str
    token: SecretStr
    response_grace_seconds: float
    recovery_seconds: float
    probe_interval_seconds: float

    def run(self, request: CommandRequest) -> CommandResult:
        """Run ``request`` in the sandbox and return what it did; raise ``SandboxError`` if it cannot."""
        response = self._post(RUN_PATH, request, request.timeout_seconds + self.response_grace_seconds)
        return CommandResult.model_validate_json(response.content)

    def reset(self, keep: Path) -> None:
        """Clear what an earlier episode left in the sandbox, all but ``keep``, the new episode's checkout.

        A sandbox an earlier episode's agents killed is restarting: wait up to ``recovery_seconds`` for it to
        answer, then raise ``SandboxError``, so agent code never runs anywhere else.
        """
        for attempt in tenacity.Retrying(
            retry=tenacity.retry_if_exception(_is_unreachable),
            stop=tenacity.stop_after_delay(self.recovery_seconds),
            wait=tenacity.wait_fixed(self.probe_interval_seconds),
            reraise=True,
        ):
            with attempt:
                self._post(RESET_PATH, ResetRequest(keep=keep), self.response_grace_seconds)

    def _post(self, path: str, body: BaseModel, timeout_seconds: float) -> httpx2.Response:
        try:
            response = httpx2.post(
                f"{self.url}{path}",
                content=body.model_dump_json(),
                headers={
                    "authorization": f"Bearer {self.token.get_secret_value()}",
                    "content-type": "application/json",
                },
                timeout=timeout_seconds,
            )
            response.raise_for_status()
        except httpx2.HTTPError as error:
            raise SandboxError(f"the sandbox did not run it: {type(error).__name__}: {error}") from error
        return response


def _is_unreachable(error: BaseException) -> bool:
    """Whether ``error`` is a sandbox that did not answer at all, as one restarting does."""
    return isinstance(error, SandboxError) and isinstance(error.__cause__, httpx2.TransportError)


def connect_sandbox(settings: LocArenaSettings) -> SandboxClient | None:
    """The sandbox's client in the episode container; none elsewhere, where agent code runs here."""
    url = sandbox_url()
    if url is None:
        return None
    return SandboxClient(
        url,
        SandboxSecrets(_secrets_dir=settings.gateway.secrets_dir).sandbox_token,
        settings.stack.sandbox_response_grace_seconds,
        settings.stack.sandbox_recovery_seconds,
        settings.stack.gateway_health_interval_seconds,
    )


def build_server_settings(settings: LocArenaSettings) -> ServerSettings:
    """What the sandbox's command server runs on, taken from the run's settings."""
    return ServerSettings(
        port=settings.stack.sandbox_port,
        checkouts_directory=settings.stack.checkouts_directory,
        scratch_directories=settings.stack.sandbox_scratch_directories,
        output_limit_bytes=settings.stack.command_output_limit_bytes,
        secrets_dir=settings.gateway.secrets_dir,
    )
