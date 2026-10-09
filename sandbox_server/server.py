"""The command server: runs a command in a directory under the checkouts for a holder of the sandbox token.

It sees the volume of the checkouts and nothing else of the episode: not the event logs, not the run configs,
not the provider key.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import os
import shutil
import signal
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from sandbox_server.command import run_command
from sandbox_server.confinement import resolve_inside
from sandbox_server.wire import RESET_PATH, RUN_PATH, CommandRequest, CommandResult, ResetRequest

ALL_INTERFACES = "0.0.0.0"
MINIMUM_TOKEN_LENGTH = 32
CONTAINER_MARKER = Path("/.dockerenv")  # Docker creates it in every container
# At module level: the route's annotations are resolved in this module's namespace, not the app factory's.
BEARER = HTTPBearer(auto_error=False)
# A sandbox token: its caller presents it, and an empty or short one is refused wherever it is read.
SandboxToken = Annotated[SecretStr, Field(min_length=MINIMUM_TOKEN_LENGTH)]


class ServerSettings(BaseModel):
    """What the command server needs to run, given as JSON in its command: it mounts no config."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: int = Field(ge=1, le=65535, description="The port the command server listens on.")
    checkouts_directory: Path = Field(
        description="Where the checkouts' volume is mounted; commands run only in directories under it.",
    )
    scratch_directories: tuple[Path, ...] = Field(
        description="The server's HOME and temporary directories, emptied on each reset so an earlier "
        "episode's agents leave nothing there for the next.",
    )
    output_limit_bytes: PositiveInt = Field(
        description="Bytes of a command's stdout and of its stderr kept, counted from the end.",
    )
    secrets_dir: Path = Field(
        description="Where compose mounts this sandbox's own token, a file named sandbox_token.",
    )


class SandboxSecrets(BaseSettings):
    """This sandbox's own token, read from the file compose mounts in the secrets directory."""

    model_config = SettingsConfigDict(frozen=True)

    sandbox_token: SandboxToken = Field(description="The token a caller of this command server presents.")


def create_sandbox_app(
    settings: ServerSettings,
    token: SecretStr,
    *,
    after_command: Callable[[], None] | None = None,
) -> FastAPI:
    """The command server: runs a command under ``settings.checkouts_directory`` for a holder of ``token``.

    ``after_command`` runs once each command has ended; in the container it ends what the command left behind.
    """

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER)]) -> None:
        presented = credentials.credentials.encode() if credentials else b""
        if not hmac.compare_digest(presented, token.get_secret_value().encode()):
            raise HTTPException(HTTPStatus.UNAUTHORIZED, "a sandbox token is required")

    def confine(directory: Path) -> Path:
        try:
            return resolve_inside(settings.checkouts_directory, directory)
        except ValueError as error:
            raise HTTPException(HTTPStatus.FORBIDDEN, str(error)) from error

    # No framework pages: they would answer without the token.
    app = FastAPI(dependencies=[Depends(authenticate)], docs_url=None, redoc_url=None, openapi_url=None)

    @app.post(RUN_PATH)
    def run(request: CommandRequest) -> CommandResult:
        confined = request.model_copy(update={"directory": confine(request.directory)})
        result = run_command(confined, settings.output_limit_bytes, os.environ)
        if after_command:
            after_command()
        return result

    @app.post(RESET_PATH)
    def reset(request: ResetRequest) -> None:
        """Remove what an earlier episode could leave: scratch files, every other entry of the checkouts."""
        keep = confine(request.keep)
        root = settings.checkouts_directory.resolve()
        if keep == root:
            raise HTTPException(HTTPStatus.FORBIDDEN, "keep one checkout below the checkouts, not all")
        scratch = [entry for directory in settings.scratch_directories for entry in directory.iterdir()]
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


def main() -> None:
    """Serve the command server: ``python -m sandbox_server '<ServerSettings as JSON>'``."""
    parser = argparse.ArgumentParser(prog="sandbox_server")
    parser.add_argument("settings", help="The server's settings as JSON; the sandbox mounts no config.")
    settings = ServerSettings.model_validate_json(parser.parse_args().settings)
    if not CONTAINER_MARKER.exists():
        parser.error("it runs only in its container: after each command it kills every other process")
    token = SandboxSecrets(_secrets_dir=settings.secrets_dir).sandbox_token
    app = create_sandbox_app(settings, token, after_command=end_leftover_processes)
    uvicorn.run(app, host=ALL_INTERFACES, port=settings.port)
