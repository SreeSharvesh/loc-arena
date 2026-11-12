"""A deployed service's supervisor: it runs the service as its child, on the release its deployer hands it.

A service the scenario's deployer lists (``deploys:``) runs as ``python -m loc_arena.release_supervisor <run
config> <entry name>``, which starts the service's own module as a child process and serves two routes on
``deploy.release_port`` to one trusted caller, the deployer, which holds this supervisor's token. A release is
each company repo's tree as a tar. The supervisor unpacks one beside the release the service runs
(``deploy.releases_directory``, in the container's own layer), ends the service's process, and starts it with
the new release's repos ahead of the image's on ``PYTHONPATH``. Once the service answers on its port, the
``current`` link moves to the new release and the others are removed; if it does not answer within
``deploy.health_seconds``, the link stays, and the service starts again on the release it ran.

Before it starts the service, the supervisor ends every process the last one left: its process group, then
whatever else runs in the container, so no process of an earlier release holds the service's port or runs
beside a later one. It imports nothing of a release, starts the service again when its process ends by
itself, and ends it when its container stops. It refuses a request on its head, before any of its body is
read. Its own lines on stdout never start with ``{``, so none is read as a record of the service's.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from http import HTTPStatus
from pathlib import Path
from typing import IO, Annotated

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import Base64Bytes, BaseModel, ConfigDict, Field, SecretStr, StringConstraints, TypeAdapter
from sandbox_server.server import CONTAINER_MARKER, end_leftover_processes
from sandbox_server.trusted import ALL_INTERFACES, ServerToken, resolve_addresses
from scenarios.loader import EngineModule
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from loc_arena.config import load_run_config
from loc_arena.deploy_pool import RELEASE_TOKEN_FILE, find_deployer
from loc_arena.merged_main import write_merged_checkout
from loc_arena.settings import DeploySettings

RELEASE_PATH = "/release"  # POST: take a release; GET: the release the service runs
CURRENT = "current"  # in the releases directory: the link to the release the service runs
LOOPBACK = "127.0.0.1"
READ_BYTES = 4096  # of the service's stderr, at a time
STDERR_HELD = "(its stderr is still open: its errors may be incomplete)"
LOGGED_READS = 256  # reads of its stderr that wait to be logged; a read past them is kept, and not logged
BODY_MARGIN_BYTES = 4096  # what a release's request holds beside its archives: its name and theirs
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


def limit_release_request(max_release_bytes: int) -> int:
    """The longest request that hands over a release of ``max_release_bytes``: its archives in base64."""
    return max_release_bytes * 4 // 3 + BODY_MARGIN_BYTES


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
        end_leftovers: Callable[[], None],
    ) -> None:
        """Run ``command``, listening on ``port``, in ``environment``; ``settings`` place its releases.

        ``end_leftovers`` ends every process the service left outside its own process group.
        """
        self._command, self._port, self._settings = list(command), port, settings
        self._environment, self._end_leftovers = dict(environment), end_leftovers
        self._lock = threading.Lock()  # one change of the process at a time
        self._stopped = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._errors = bytearray()  # the end of the running process's stderr
        self._reader = threading.Thread()  # of that stderr
        self._to_log: queue.Queue[bytes] = queue.Queue(LOGGED_READS)

    def start(self) -> None:
        """Start the service on the release it ran last, and keep it running until ``stop``."""
        self._settings.releases_directory.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._spawn(self._find_current())
        threading.Thread(target=self._keep_running, daemon=True).start()
        threading.Thread(target=_log_errors, args=(self._to_log,), daemon=True).start()

    def stop(self) -> None:
        """End the service's process, and start it no more."""
        self._stopped.set()
        with self._lock:
            self._end()

    def read_state(self) -> ReleaseState:
        """The release the service runs, once no hand-over is under way, and whether it answers."""
        with self._lock:
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
            failure = None if self._answers(self._settings.health_seconds) else ""
            if failure is None:
                try:
                    link = releases / f".{CURRENT}"
                    link.unlink(missing_ok=True)
                    link.symlink_to(unpacked.name)
                    link.replace(releases / CURRENT)  # atomic: a reader finds the old release or the new
                except OSError as error:
                    failure = f"the release could not be made current: {error}"
            if failure is None:
                _remove_all_but(releases, unpacked)
                return ReleaseState(release=unpacked.name, serving=True)
            self._end()  # before its errors are read
            detail = failure or self._describe_failure()
            shutil.rmtree(unpacked, ignore_errors=True)
            self._spawn(previous)
            serving = self._answers(self._settings.health_seconds)
            return ReleaseState(release=_name(previous), serving=serving, detail=detail)

    def _describe_failure(self) -> str:
        """Why the service did not answer: its last errors, said to be incomplete while its stderr is open."""
        held = self._reader.is_alive()  # asked first: a reader that ends after this has every error in
        errors = bytes(self._errors).decode(errors="replace") or f"no answer on port {self._port}"
        return f"{errors}\n{STDERR_HELD}" if held else errors

    def _find_current(self) -> Path | None:
        """The directory of the release the ``current`` link names; none on the image's code."""
        link = self._settings.releases_directory / CURRENT
        return link.resolve() if link.is_symlink() and link.exists() else None

    def _spawn(self, release: Path | None) -> None:
        """Start the service with the repos of ``release``, as resolved directories, ahead of the image's.

        It leads a process group of its own, so the group ends with it.
        """
        repos = sorted(str(repo) for repo in release.iterdir() if repo.is_dir()) if release else []
        image = self._environment.get("PYTHONPATH", "")
        environment = {**self._environment, "PYTHONPATH": os.pathsep.join([*repos, image])}
        process = subprocess.Popen(  # noqa: S603 - the service's own module, named by the scenario
            self._command,
            env=environment,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        errors = bytearray()
        read = (process.stderr, errors, self._settings.error_bytes, self._to_log)
        self._process, self._errors = process, errors
        self._reader = threading.Thread(target=_read_errors, args=read, daemon=True)
        self._reader.start()
        print(f"supervisor: started the service on {_name(release) or 'the image'}", flush=True)  # noqa: T201

    def _end(self) -> None:
        """End the service: ask its process to end, kill its group, then end whatever else it left."""
        process = self._process
        if process is not None:
            if process.poll() is None:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(self._settings.stop_seconds)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)  # the group it leads: its children too
            process.wait()
        self._end_leftovers()
        if self._reader.is_alive():  # none before the service first started
            self._reader.join(self._settings.stop_seconds)  # until its stderr closes, or something holds it

    def _answers(self, wait_seconds: float) -> bool:
        """Whether the service's own process is alive and its port accepts within ``wait_seconds``."""
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
        while not self._stopped.wait(self._settings.watch_seconds):
            with self._lock:
                if self._stopped.is_set() or self._process is None or self._process.poll() is None:
                    continue
                print(f"supervisor: the service ended with {self._process.returncode}", flush=True)  # noqa: T201
                self._end()
                try:
                    self._spawn(self._find_current())
                except OSError as error:  # no process to be had now: the next look tries again
                    print(f"supervisor: the service did not start: {error}", flush=True)  # noqa: T201


def _read_errors(stream: IO[bytes], kept: bytearray, kept_bytes: int, to_log: queue.Queue[bytes]) -> None:
    """Keep the last ``kept_bytes`` of a process's stderr in ``kept``, and hand each read on ``to_log``.

    Reading never waits for the logging: what a full queue cannot take is kept and not logged.
    """
    while chunk := stream.read1(READ_BYTES):  # ty: ignore[unresolved-attribute] - a pipe's buffered reader
        kept += chunk
        del kept[:-kept_bytes]
        with contextlib.suppress(queue.Full):
            to_log.put_nowait(chunk)


def _log_errors(to_log: queue.Queue[bytes]) -> None:
    """Write what the service's processes wrote to stderr on this container's, as it comes."""
    while True:
        sys.stderr.write(to_log.get().decode(errors="replace"))
        sys.stderr.flush()


def _name(release: Path | None) -> str | None:
    """The name of ``release``; none for the image's code."""
    return None if release is None else release.name


def _remove_all_but(releases: Path, kept: Path) -> None:
    """Remove every release of ``releases`` but ``kept``: the service runs one, and none is used again."""
    for entry in releases.iterdir():
        if entry.is_dir() and not entry.is_symlink() and entry != kept:
            shutil.rmtree(entry, ignore_errors=True)


def admit_deployer_alone(app: ASGIApp, deployer: str, token: SecretStr, max_request_bytes: int) -> ASGIApp:
    """``app`` behind what it asks of a caller, checked on a request's head before any body is read.

    The request comes from an address ``deployer`` resolves to, carries ``token``, and one that hands over
    a release declares a length within ``max_request_bytes``.
    """

    def refuse(scope: Scope) -> tuple[HTTPStatus, str] | None:
        headers, client = Headers(scope=scope), scope.get("client")
        # Looked up on each request: the deployer's address is known only once it is up.
        if client is None or client[0] not in resolve_addresses(deployer):
            return HTTPStatus.FORBIDDEN, f"only the {deployer} service may call this supervisor"
        scheme, _, presented = headers.get("authorization", "").partition(" ")
        held = token.get_secret_value().encode()
        if scheme.lower() != "bearer" or not hmac.compare_digest(presented.encode(), held):
            return HTTPStatus.UNAUTHORIZED, "a supervisor's token is required"
        declared = headers.get("content-length", "")
        if scope["method"] == "POST" and not (declared.isascii() and declared.isdecimal()):
            return HTTPStatus.LENGTH_REQUIRED, "a release declares its length"
        if scope["method"] == "POST" and int(declared) > max_request_bytes:
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "the release is larger than a release may be"
        return None

    async def admit(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and (refusal := refuse(scope)) is not None:
            await JSONResponse({"detail": refusal[1]}, refusal[0])(scope, receive, send)
            return
        await app(scope, receive, send)

    return admit


def create_supervisor_app(
    service: SupervisedService,
    token: SecretStr,
    deployer: str,
    max_release_bytes: int,
) -> ASGIApp:
    """The supervisor of ``service``: it answers ``deployer`` alone, holding ``token``."""

    @contextlib.asynccontextmanager
    async def run_service(_: FastAPI) -> AsyncIterator[None]:
        service.start()
        yield
        service.stop()

    app = FastAPI(lifespan=run_service, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get(RELEASE_PATH)
    def read_release() -> ReleaseState:
        return service.read_state()

    @app.post(RELEASE_PATH)
    def take_release(release: Release) -> ReleaseState:
        try:
            return service.take(release)
        except ReleaseTakenError as taken:
            raise HTTPException(HTTPStatus.CONFLICT, str(taken)) from taken

    return admit_deployer_alone(app, deployer, token, limit_release_request(max_release_bytes))


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
    if not CONTAINER_MARKER.exists():
        raise SystemExit("it runs only in its container: it kills every process its service leaves behind")
    command = [sys.executable, "-m", entry.source.name, *arguments]
    service = SupervisedService(command, entry.port, settings, os.environ, end_leftover_processes)
    app = create_supervisor_app(service, token, deployer.name, settings.max_release_bytes)
    # The caller is the peer that connected: no header names another, whoever sends it.
    uvicorn.run(
        app,
        host=ALL_INTERFACES,
        port=settings.release_port,
        log_level="warning",
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
