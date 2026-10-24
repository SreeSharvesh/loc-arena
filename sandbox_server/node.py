"""A node's daemon: it runs the node's one job as a real, long-lived process for the job service.

Unlike the command server, which runs a command and waits, the daemon starts a process and returns: the
process runs until it exits, the job service stops it, or a reset ends it. The daemon says whether it is still
running and how it ended, serves the tails of its output, and writes each process fact (a job's start and its
exit) as a JSON line on its own stdout, which the host copies out and the sealed log alone keeps. It takes
calls from the job service alone, holding the node's token, and runs the job in an empty workspace with the
checkouts mounted read-only and the checkout's repos on its ``PYTHONPATH`` unless the job brings its own, as
the seed's jobs do, so an agent's job runs the code an agent put in the shared checkout and cannot change
what another job runs. The job's process gets the job's own
credentials the service sends, and where the inference gateway is, the key to call it and the model it serves
jobs: the node's token, until the gateway admits a job's own credential. It imports nothing of the harness.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from sandbox_server.command import API_KEY_VARIABLE, read_tail
from sandbox_server.node_wire import (
    OUTPUT_PATH,
    RESET_PATH,
    START_PATH,
    STATUS_PATH,
    STOP_PATH,
    NodeProcessOutput,
    NodeProcessState,
    NodeProcessStatus,
    StartJob,
)
from sandbox_server.server import CONTAINER_MARKER
from sandbox_server.trusted import ALL_INTERFACES, ServerToken, admit_trusted_caller, authenticate_token

STOP_GRACE_SECONDS = 5.0  # a stopped process is waited for this long so a following status sees it exited
GATEWAY_URL_VARIABLE = "MERIDIAN_GATEWAY_URL"  # the inference gateway's API, as a job calls it
GATEWAY_TOKEN_VARIABLE = "MERIDIAN_GATEWAY_TOKEN"  # its key: the node's token, unless the job brings its own
GATEWAY_MODEL_VARIABLE = "MERIDIAN_GATEWAY_MODEL"  # the model a job calls it for


class NodeServerSettings(BaseModel):
    """What a node's daemon runs on, given as JSON in its command: it mounts no config."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: int = Field(ge=1, le=65535, description="The port the daemon listens on.")
    node_id: str = Field(min_length=1, description="The node, as its facts and the job service name it.")
    workspace_directory: Path = Field(description="The job's writable workspace, emptied before each job.")
    output_limit_bytes: PositiveInt = Field(description="Bytes of the job's stdout and of its stderr kept.")
    secrets_dir: Path = Field(description="Where compose mounts this node's token, named sandbox_token.")
    trusted_caller: str = Field(description="The one caller served, the job service: any other is refused.")
    gateway_url: str = Field(
        description="The inference gateway's API as a job calls it, on the runners' route: a job calls it as "
        "its node, with the node's token, until the gateway admits a job's own credential.",
    )
    gateway_model: str = Field(
        min_length=1,
        description="The model a job is given to call the gateway for: one the runners' route allows.",
    )
    checkouts_directory: Path = Field(
        description="Where the checkouts' volume is mounted, read-only: the repos of the checkout the "
        "episode seeded below it are on each job's PYTHONPATH.",
    )


def list_checkout_repos(checkouts: Path) -> list[Path]:
    """The repos a job imports: each directory of the checkout the episode seeded below ``checkouts``.

    The episode seeds its checkout as ``<home>/checkout/<repo>``, one home per play, and moves it out after;
    the sandboxes' reset removes any other home, so at most one checkout is there while a job runs, and
    without one a job imports nothing of the company.
    """
    return sorted(repo for repo in checkouts.glob("*/checkout/*") if repo.is_dir())


class NodeSecrets(BaseSettings):
    """This node's own token, read from the file compose mounts in the secrets directory."""

    model_config = SettingsConfigDict(frozen=True)

    sandbox_token: ServerToken = Field(description="The token a caller of this daemon presents.")


class NodeProcess:
    """The one job a node runs at a time, as a long-lived process in the node's workspace."""

    def __init__(
        self,
        settings: NodeServerSettings,
        write_fact: Callable[[str], None],
        node_token: SecretStr,
    ) -> None:
        """Run jobs in ``settings.workspace_directory`` and write each process fact with ``write_fact``.

        Each job is given ``node_token`` as its key to the gateway unless it brings its own.
        """
        self._settings = settings
        self._write_fact = write_fact
        self._node_token = node_token
        self._lock = threading.Lock()
        self._job_id: str | None = None
        self._owner: str | None = None  # the principal whose job it is, named on every fact of that job
        self._attempt = 1  # which run of the job it is: one more each time a drain took it off a node
        self._process: subprocess.Popen[bytes] | None = None
        self._streams: tuple[object, object] | None = None  # the job's open stdout and stderr files
        self._exit_code: int | None = None
        self._final_output: NodeProcessOutput | None = None  # the tails kept once the process has ended

    def start(self, job: StartJob) -> None:
        """Start ``job``'s command, on a node holding no job since its last reset; 409 otherwise."""
        with self._lock:
            if self._job_id is not None:
                raise HTTPException(HTTPStatus.CONFLICT, f"the node already holds job {self._job_id!r}")
            workspace = self._empty_workspace()
            repos = list_checkout_repos(self._settings.checkouts_directory)
            environment = {
                GATEWAY_URL_VARIABLE: self._settings.gateway_url,
                GATEWAY_TOKEN_VARIABLE: self._node_token.get_secret_value(),
                GATEWAY_MODEL_VARIABLE: self._settings.gateway_model,
                "PYTHONPATH": os.pathsep.join(map(str, repos)),
                **{name: secret.get_secret_value() for name, secret in job.environment.items()},
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(workspace),
                "TMPDIR": str(workspace),
            }
            # Appended to, so reading a tail, which moves the offset the job shares, never moves its writes.
            stdout = (workspace / ".stdout").open("a+b")
            stderr = (workspace / ".stderr").open("a+b")
            try:
                self._process = subprocess.Popen(
                    job.argv,
                    cwd=workspace,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            except OSError as error:  # no such command, or not one it may run: the job never started
                stdout.close()
                stderr.close()
                detail = f"cannot start {job.argv[0]!r}: {error.strerror}"
                raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, detail) from error
            self._job_id, self._owner, self._streams = job.job_id, job.owner, (stdout, stderr)
            self._attempt = job.attempt
            self._exit_code, self._final_output = None, None
        self._fact("start", job_id=job.job_id, owner=job.owner, attempt=job.attempt, argv=list(job.argv))

    def status(self) -> NodeProcessStatus:
        """The node's job and how its process is, reaping it the first time it is seen ended."""
        with self._lock:
            self._reap()
            if self._job_id is None:
                return NodeProcessStatus(job_id=None, state=NodeProcessState.IDLE)
            state = NodeProcessState.RUNNING if self._exit_code is None else NodeProcessState.EXITED
            return NodeProcessStatus(job_id=self._job_id, state=state, exit_code=self._exit_code)

    def output(self) -> NodeProcessOutput:
        """The tails of the job's stdout and stderr, live while it runs, else as they were when it ended."""
        with self._lock:
            if self._final_output is not None:
                return self._final_output
            return self._read_output()

    def stop(self) -> None:
        """End the node's job's process and every process it started, then wait briefly for it to go."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(STOP_GRACE_SECONDS)
            self._reap()

    def reset(self) -> None:
        """End whatever runs on the node and empty its workspace, so it can take the next job."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self._kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._process.wait(STOP_GRACE_SECONDS)
            self._reap()
            self._close_streams()
            self._job_id, self._owner, self._process = None, None, None
            self._exit_code, self._final_output = None, None
            self._empty_workspace()

    def _reap(self) -> None:
        """If the process has ended and its exit is unrecorded, keep its code and output, write the fact."""
        if self._process is None or self._exit_code is not None:
            return
        code = self._process.poll()
        if code is None:
            return
        self._exit_code = code
        self._final_output = self._read_output()
        self._fact("exit", job_id=self._job_id, owner=self._owner, attempt=self._attempt, exit_code=code)

    def _kill(self) -> None:
        if self._process is None:
            return
        with contextlib.suppress(ProcessLookupError):  # the whole session ended meanwhile
            os.killpg(self._process.pid, signal.SIGKILL)

    def _read_output(self) -> NodeProcessOutput:
        if self._streams is None:
            return NodeProcessOutput(job_id=self._job_id, stdout="", stderr="")
        stdout, stderr = self._streams
        return NodeProcessOutput(
            job_id=self._job_id,
            stdout=read_tail(stdout, self._settings.output_limit_bytes),  # ty: ignore[invalid-argument-type]
            stderr=read_tail(stderr, self._settings.output_limit_bytes),  # ty: ignore[invalid-argument-type]
        )

    def _close_streams(self) -> None:
        for stream in self._streams or ():
            with contextlib.suppress(OSError):
                stream.close()  # ty: ignore[unresolved-attribute]
        self._streams = None

    def _empty_workspace(self) -> Path:
        workspace = self._settings.workspace_directory
        workspace.mkdir(parents=True, exist_ok=True)
        for entry in workspace.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    entry.unlink(missing_ok=True)
        return workspace

    def _fact(self, phase: str, **payload: object) -> None:
        """Write one process fact on stdout: a job's start or its exit, named by node and time."""
        fact = {"node": self._settings.node_id, "phase": phase, **payload}
        self._write_fact(json.dumps({"wall_ts": datetime.now(UTC).isoformat(), "process": fact}))


def create_node_app(settings: NodeServerSettings, token: SecretStr, process: NodeProcess) -> FastAPI:
    """The daemon over ``process``: start, status, output, stop and reset, for its trusted caller."""
    admit = admit_trusted_caller(settings.trusted_caller, "node daemon")
    authenticate = authenticate_token(token)
    app = FastAPI(
        dependencies=[Depends(admit), Depends(authenticate)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.post(START_PATH, status_code=HTTPStatus.ACCEPTED)
    def start(job: StartJob) -> NodeProcessStatus:
        process.start(job)
        return process.status()

    @app.get(STATUS_PATH)
    def status() -> NodeProcessStatus:
        return process.status()

    @app.get(OUTPUT_PATH)
    def output() -> NodeProcessOutput:
        return process.output()

    @app.post(STOP_PATH)
    def stop() -> NodeProcessStatus:
        process.stop()
        return process.status()

    @app.post(RESET_PATH)
    def reset() -> None:
        process.reset()

    return app


def main() -> None:
    """Serve a node's daemon: ``python -m sandbox_server.node '<NodeServerSettings as JSON>'``."""
    parser = argparse.ArgumentParser(prog="sandbox_server.node")
    parser.add_argument("settings", help="The daemon's settings as JSON; the node mounts no config.")
    settings = NodeServerSettings.model_validate_json(parser.parse_args().settings)
    if not CONTAINER_MARKER.exists():
        parser.error(
            "it runs only in its container: it kills the job's whole process group on stop and reset",
        )
    token = NodeSecrets(_secrets_dir=settings.secrets_dir).sandbox_token
    environment = dict(os.environ)
    environment.pop(API_KEY_VARIABLE, None)  # the provider key never reaches a job
    os.environ.clear()
    os.environ.update(environment)
    process = NodeProcess(settings, lambda line: print(line, flush=True), token)
    # No access log: the job service polls every second, and the log is the node's record of process facts.
    app = create_node_app(settings, token, process)
    uvicorn.run(app, host=ALL_INTERFACES, port=settings.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
