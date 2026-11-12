"""A deployed service's supervisor: it runs the service as its child, on the release its deployer hands it.

A service the scenario's deployer lists (``deploys:``) runs as ``python -m loc_arena.release_supervisor <run
config> <entry name>``, which starts the service's own module as a child process and serves two routes on
``deploy.release_port`` to one trusted caller, the deployer, which holds this supervisor's token. A release is
each company repo's tree as a tar. The supervisor unpacks one beside the release the service runs
(``deploy.releases_directory``, in the container's own layer), ends the service's process, and starts it with
the new release's repos ahead of the image's on ``PYTHONPATH``. Once the service answers on its port, the
``current`` link moves to the new release and the others are removed; if it does not answer within
``deploy.health_seconds``, the link stays, and the service starts again on the release it ran. The supervisor
imports nothing of a release, starts the service again when its process ends by itself, and ends it when its
container stops. Its own lines on stdout never start with ``{``, so none is read as a record of the service's.
"""

from __future__ import annotations

import collections
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from http import HTTPStatus
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import Base64Bytes, BaseModel, ConfigDict, Field, SecretStr, StringConstraints, TypeAdapter
from sandbox_server.trusted import ALL_INTERFACES, ServerToken, admit_trusted_caller, authenticate_token
from scenarios.loader import EngineModule

from loc_arena.config import load_run_config
from loc_arena.deploy_pool import RELEASE_TOKEN_FILE, find_deployer
from loc_arena.merged_main import write_merged_checkout
from loc_arena.settings import DeploySettings

RELEASE_PATH = "/release"  # POST: take a release; GET: the release the service runs
CURRENT = "current"  # in the releases directory: the link to the release the service runs
LOOPBACK = "127.0.0.1"
STOP_GRACE_SECONDS = 5.0  # a process told to end is waited for this long, then killed
WATCH_SECONDS = 0.5  # how often the supervisor looks whether the service's process has ended by itself
ERROR_TAIL_LINES = 40  # of a failed process's errors, what a refused release's answer holds
# A release or a repo names a directory: one path component, never a relative one.
Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]


class Release(BaseModel):
    """What the deployer hands over: a release's name, and each company repo's tree as a tar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Name
    archives: dict[Name, Base64Bytes] = Field(min_length=1)


class ReleaseState(BaseModel):
    """The release the service runs, the image's code when none, and whether it answers on its port."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    release: str | None
    serving: bool
    detail: str = ""  # why the release last handed over is not the one running: the end of its errors


class ReleaseTakenError(Exception):
    """A release handed over under the name of one the supervisor holds."""


class SupervisedService:
    """The service's process, and the release it runs."""

    def __init__(
        self,
        command: Sequence[str],
        port: int,
        settings: DeploySettings,
        environment: Mapping[str, str],
    ) -> None:
        """Run ``command``, listening on ``port``, in ``environment``; ``settings`` place its releases."""
        self._command, self._port, self._settings = list(command), port, settings
        self._environment = dict(environment)
        self._lock = threading.Lock()  # one change of the process at a time
        self._stopped = threading.Event()
        self._errors: collections.deque[str] = collections.deque(maxlen=ERROR_TAIL_LINES)
        self._process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        """Start the service on the release it ran last, and keep it running until ``stop``."""
        self._settings.releases_directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._spawn(self._find_current())
        threading.Thread(target=self._keep_running, daemon=True).start()

    def stop(self) -> None:
        """End the service's process, and start it no more."""
        self._stopped.set()
        with self._lock:
            self._end()

    def read_state(self) -> ReleaseState:
        """The release the service runs now, and whether it answers."""
        current = self._find_current()
        return ReleaseState(release=_name(current), serving=self._answers(wait_seconds=0))

    def take(self, release: Release) -> ReleaseState:
        """Have the service run ``release``; the state says which release it runs once this returns.

        The release is unpacked whole before the service's process ends. If the service does not answer on
        it in time, it starts again on the release it ran, and ``detail`` holds the end of its errors.
        """
        releases = self._settings.releases_directory
        with self._lock:
            if (releases / release.name).exists():
                raise ReleaseTakenError(f"this service holds a release {release.name!r} already")
            previous = self._find_current()
            incoming = releases / f".{release.name}"
            write_merged_checkout(incoming, release.archives)
            unpacked = incoming.replace(releases / release.name)
            self._end()
            self._spawn(unpacked)
            if self._answers(self._settings.health_seconds):
                link = releases / f".{CURRENT}"
                link.unlink(missing_ok=True)
                link.symlink_to(unpacked.name)
                link.replace(releases / CURRENT)  # atomic: a reader finds the old release or the new
                _remove_all_but(releases, unpacked)
                return ReleaseState(release=unpacked.name, serving=True)
            detail = "".join(self._errors) or f"it did not answer on port {self._port}"
            self._end()
            shutil.rmtree(unpacked, ignore_errors=True)
            self._spawn(previous)
            serving = self._answers(self._settings.health_seconds)
            return ReleaseState(release=_name(previous), serving=serving, detail=detail)

    def _find_current(self) -> Path | None:
        """The directory of the release the ``current`` link names; none on the image's code."""
        link = self._settings.releases_directory / CURRENT
        return link.resolve() if link.is_symlink() and link.exists() else None

    def _spawn(self, release: Path | None) -> None:
        """Start the service with the repos of ``release``, as resolved directories, ahead of the image's."""
        repos = sorted(str(repo) for repo in release.iterdir() if repo.is_dir()) if release else []
        image = self._environment.get("PYTHONPATH", "")
        environment = {**self._environment, "PYTHONPATH": os.pathsep.join([*repos, image])}
        self._errors.clear()
        self._process = subprocess.Popen(  # noqa: S603 - the service's own module, named by the scenario
            self._command,
            env=environment,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        threading.Thread(target=self._copy_errors, args=(self._process,), daemon=True).start()
        print(f"supervisor: started the service on {_name(release) or 'the image'}", flush=True)  # noqa: T201

    def _copy_errors(self, process: subprocess.Popen[str]) -> None:
        """Pass the process's stderr on to this container's, keeping its last lines."""
        assert process.stderr is not None  # noqa: S101 - spawned with a pipe
        for line in process.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()
            if process is self._process:
                self._errors.append(line)

    def _end(self) -> None:
        """End the service's process: ask it to, wait, then kill it."""
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _answers(self, wait_seconds: float) -> bool:
        """Whether the service's own process accepts a connection on its port within ``wait_seconds``."""
        deadline = time.monotonic() + wait_seconds
        while True:
            if self._process is None or self._process.poll() is not None:
                return False  # its process has ended: nothing of this release will answer
            with contextlib.suppress(OSError), socket.create_connection((LOOPBACK, self._port), 1):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def _keep_running(self) -> None:
        """Start the service again, on the release it ran, whenever its process has ended by itself."""
        while not self._stopped.wait(WATCH_SECONDS):
            with self._lock:
                if not self._stopped.is_set() and self._process and self._process.poll() is not None:
                    print(f"supervisor: the service ended with {self._process.returncode}", flush=True)  # noqa: T201
                    self._spawn(self._find_current())


def _name(release: Path | None) -> str | None:
    """The name of ``release``; none for the image's code."""
    return None if release is None else release.name


def _remove_all_but(releases: Path, kept: Path) -> None:
    """Remove every release of ``releases`` but ``kept``: the service runs one, and none is used again."""
    for entry in releases.iterdir():
        if entry.is_dir() and not entry.is_symlink() and entry != kept:
            shutil.rmtree(entry, ignore_errors=True)


def create_supervisor_app(
    service: SupervisedService,
    token: SecretStr,
    deployer: str,
    max_release_bytes: int,
) -> FastAPI:
    """The supervisor of ``service``: it answers ``deployer`` alone, holding ``token``."""

    @contextlib.asynccontextmanager
    async def run_service(_: FastAPI) -> AsyncIterator[None]:
        service.start()
        yield
        service.stop()

    def refuse_a_large_release(request: Request) -> None:
        declared = request.headers.get("Content-Length", "")
        if not (declared.isascii() and declared.isdecimal()):
            raise HTTPException(HTTPStatus.LENGTH_REQUIRED, "a release declares its length")
        if int(declared) > max_release_bytes:
            raise HTTPException(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"a release holds at most {max_release_bytes} bytes",
            )

    app = FastAPI(
        lifespan=run_service,
        dependencies=[
            Depends(admit_trusted_caller(deployer, "supervisor")),
            Depends(authenticate_token(token)),
        ],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get(RELEASE_PATH)
    def read_release() -> ReleaseState:
        return service.read_state()

    @app.post(RELEASE_PATH, dependencies=[Depends(refuse_a_large_release)])
    def take_release(release: Release) -> ReleaseState:
        try:
            return service.take(release)
        except ReleaseTakenError as taken:
            raise HTTPException(HTTPStatus.CONFLICT, str(taken)) from taken

    return app


def main(arguments: list[str]) -> None:
    """Supervise entry ``arguments[1]`` of the run config at ``arguments[0]``, for the scenario's deployer."""
    config = load_run_config(arguments[0])
    entry = next(service for service in config.live_services if service.name == arguments[1])
    deployer = find_deployer(config)
    if deployer is None or entry.name not in deployer.deploys:
        raise SystemExit(f"no service of {arguments[0]} deploys {entry.name!r}")
    settings = config.settings.deploy
    token_file = config.settings.gateway.secrets_dir / RELEASE_TOKEN_FILE
    token = TypeAdapter(ServerToken).validate_python(token_file.read_text().strip())
    if not isinstance(entry.source, EngineModule):
        raise SystemExit(f"{entry.name!r} is no module of the engine image, so it cannot be supervised")
    command = [sys.executable, "-m", entry.source.name, *arguments]
    service = SupervisedService(command, entry.port, settings, os.environ)
    app = create_supervisor_app(service, token, deployer.name, settings.max_release_bytes)
    uvicorn.run(app, host=ALL_INTERFACES, port=settings.release_port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main(sys.argv[1:])
