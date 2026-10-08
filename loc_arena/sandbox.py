"""The sandbox: the container where agent-written code runs in a stack run, and the episode's client of it.

The sandbox sees the volume of the checkouts and nothing else of the episode: not the event logs, not the run
configs, not the provider key. Its command server runs one command at a caller's request, in a directory under
``settings.stack.checkouts_directory``, for a caller holding the sandbox token (a compose secret mounted into
the episode and the sandbox only). In the episode container the agents' bash, run_tests and run_benchmark go
to the sandbox; elsewhere bash is refused and the other two run in this process.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import os
import shutil
import signal
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Annotated

import httpx2
import tenacity
import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from loc_arena.agent_code import CommandRequest, CommandResult, run_command
from loc_arena.gateway.proxy import ALL_INTERFACES
from loc_arena.settings import LocArenaSettings, StackSettings
from loc_arena.task import resolve_inside, sandbox_url

SANDBOX_SERVICE = "sandbox"  # the sandbox's compose service, and its host name on agent-net
TOKEN_SECRET_NAME = "sandbox_token"  # the compose secret: a file in settings.gateway.secrets_dir
TOKEN_VARIABLE = "LOC_ARENA_SANDBOX_TOKEN"  # where compose reads the secret from, on the host
RUN_PATH = "/run"
RESET_PATH = "/reset"
MINIMUM_TOKEN_LENGTH = 32
CONTAINER_MARKER = Path("/.dockerenv")  # Docker creates it in every container
# At module level: the route's annotations are resolved in this module's namespace, not the app factory's.
BEARER = HTTPBearer(auto_error=False)


class ResetRequest(BaseModel):
    """Make the sandbox ready for a new episode, whose checkout is ``keep``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keep: Path = Field(description="The new episode's checkout, under the checkouts directory.")


class SandboxError(RuntimeError):
    """The sandbox did not answer, or refused: the tool that asked gets an error result."""


class SandboxSecrets(BaseSettings):
    """The sandbox token, read from the file compose mounts in ``settings.gateway.secrets_dir``."""

    model_config = SettingsConfigDict(frozen=True)

    sandbox_token: SecretStr = Field(
        min_length=MINIMUM_TOKEN_LENGTH,
        description="The token a caller of the sandbox's command server presents; an empty one is refused.",
    )


def create_sandbox_app(
    stack: StackSettings,
    token: SecretStr,
    *,
    after_command: Callable[[], None] | None = None,
) -> FastAPI:
    """The command server: runs a command under ``stack.checkouts_directory`` for a holder of ``token``.

    ``after_command`` runs once each command has ended; in the container it ends what the command left behind.
    """

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER)]) -> None:
        presented = credentials.credentials.encode() if credentials else b""
        if not hmac.compare_digest(presented, token.get_secret_value().encode()):
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "a sandbox token is required")

    def confine(directory: Path) -> Path:
        try:
            return resolve_inside(stack.checkouts_directory, directory)
        except ValueError as error:
            raise HTTPException(HTTPStatus.FORBIDDEN, str(error)) from error

    app = FastAPI(dependencies=[Depends(authenticate)])

    @app.post(RUN_PATH)
    def run(request: CommandRequest) -> CommandResult:
        confined = request.model_copy(update={"directory": confine(request.directory)})
        result = run_command(confined, stack.command_output_limit_bytes)
        if after_command:
            after_command()
        return result

    @app.post(RESET_PATH)
    def reset(request: ResetRequest) -> None:
        """Remove what an earlier episode could leave: scratch files, every other entry of the checkouts."""
        keep = confine(request.keep)
        root = stack.checkouts_directory.resolve()
        if keep == root:
            raise HTTPException(HTTPStatus.FORBIDDEN, "keep one checkout below the checkouts, not all")
        scratch = [entry for directory in stack.sandbox_scratch_directories for entry in directory.iterdir()]
        others = [entry for entry in root.iterdir() if entry != keep and entry not in keep.parents]
        for entry in [*scratch, *others]:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                with contextlib.suppress(PermissionError):  # the image's own files, which no agent wrote
                    entry.unlink(missing_ok=True)

    return app


def end_leftover_processes() -> None:
    """Kill every process in the container but its init and this server: all the last command left behind.

    So no agent code runs between two commands, while the episode reads and writes the shared checkout: a
    process left running could swap a directory for a link to the episode's own files between the episode's
    check of a path and its use, or change the honest twin's checkout while it plays.
    """
    with contextlib.suppress(ProcessLookupError):  # nothing was left behind
        os.kill(-1, signal.SIGKILL)


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


def main() -> None:
    """Serve the command server: ``python -m loc_arena.sandbox '<the run's settings as JSON>'``."""
    parser = argparse.ArgumentParser(prog="loc_arena.sandbox")
    parser.add_argument("settings", help="The run's settings as JSON; the sandbox mounts no config.")
    settings = LocArenaSettings.model_validate_json(parser.parse_args().settings)
    if not CONTAINER_MARKER.exists():
        parser.error("it runs only in its container: after each command it kills every other process")
    token = SandboxSecrets(_secrets_dir=settings.gateway.secrets_dir).sandbox_token
    app = create_sandbox_app(settings.stack, token, after_command=end_leftover_processes)
    uvicorn.run(app, host=ALL_INTERFACES, port=settings.stack.sandbox_port)


if __name__ == "__main__":
    main()
