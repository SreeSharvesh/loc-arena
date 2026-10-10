"""The episode's clients of the sandboxes: a container per agent, where that agent's code runs in a stack run.

Each sandbox's command server (``sandbox_server``) runs one command at a caller's request, in a directory
under ``settings.stack.checkouts_directory``, for a caller holding that sandbox's own token (a compose secret
mounted into the episode and that sandbox only). In the episode container an agent's bash, run_tests and
run_benchmark go to its own sandbox; elsewhere bash is refused and the other two run in this process.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx2
import tenacity
from pydantic import BaseModel, SecretStr, TypeAdapter
from sandbox_server.server import SandboxToken, ServerSettings
from sandbox_server.wire import RESET_PATH, RUN_PATH, CommandRequest, CommandResult, ResetRequest

from loc_arena.settings import LocArenaSettings
from loc_arena.task import sandbox_url_template

TOKEN_FILE = "sandbox_token"  # each sandbox's own token, in its secrets directory: what SandboxSecrets reads
CREDENTIAL_PREFIX = "credential_"  # a live service's credential: a compose secret and its file name


class SandboxError(RuntimeError):
    """The sandbox did not answer, or refused: the tool that asked gets an error result."""


@dataclass(frozen=True)
class SandboxClient:
    """The episode's way into one agent's sandbox's command server."""

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


def token_secret_name(agent_id: str) -> str:
    """The compose secret holding the token of ``agent_id``'s sandbox: a file of that name in the episode."""
    return f"{TOKEN_FILE}_{agent_id.replace('-', '_')}"


def credential_secret_name(credential: str) -> str:
    """The compose secret holding ``credential``: a file of that name where it is mounted."""
    return f"{CREDENTIAL_PREFIX}{credential}"


def connect_sandboxes(
    settings: LocArenaSettings,
    agent_ids: Iterable[str],
) -> Mapping[str, SandboxClient] | None:
    """Each agent's sandbox client, by agent id, in the episode container; none elsewhere.

    Elsewhere agent code runs in this process. Each client holds that agent's own token, read from the
    episode's secrets; a missing or short token fails the episode, so no agent is left without its sandbox.
    """
    template = sandbox_url_template()
    if template is None:
        return None
    read_token = TypeAdapter(SandboxToken).validate_python
    return {
        agent_id: SandboxClient(
            template.format(agent=agent_id),
            # Stripped, as pydantic-settings reads the sandbox's own copy.
            read_token((settings.gateway.secrets_dir / token_secret_name(agent_id)).read_text().strip()),
            settings.stack.sandbox_response_grace_seconds,
            settings.stack.sandbox_recovery_seconds,
            settings.stack.gateway_health_interval_seconds,
        )
        for agent_id in agent_ids
    }


def reset_sandboxes(sandboxes: Mapping[str, SandboxClient], keep: Path) -> None:
    """Reset every sandbox in turn, keeping ``keep``; the first that never answers fails the episode."""
    for sandbox in sandboxes.values():
        sandbox.reset(keep)


def build_server_settings(settings: LocArenaSettings, *, trusted_caller: str) -> ServerSettings:
    """What a sandbox's command server runs on, from the run's settings; it serves ``trusted_caller``."""
    stack = settings.stack
    return ServerSettings(
        port=stack.sandbox_port,
        checkouts_directory=stack.checkouts_directory,
        scratch_directories=stack.sandbox_scratch_directories,
        output_limit_bytes=stack.command_output_limit_bytes,
        secrets_dir=settings.gateway.secrets_dir,
        trusted_caller=trusted_caller,
        command_timeout_limit_seconds=max(
            stack.shell_timeout_seconds,
            stack.run_tests_timeout_seconds,
            stack.run_benchmark_timeout_seconds,
        ),
    )
