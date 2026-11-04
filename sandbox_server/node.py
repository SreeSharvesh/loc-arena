"""A node's daemon: it runs the node's one job as a real, long-lived process for the job service.

Unlike the command server, which runs a command and waits, the daemon starts a process and returns: the
process runs until it exits, the job service stops it, or a reset ends it. The daemon says whether it is still
running and how it ended, serves the tails of its output, and writes each process fact (a job's start and its
exit) as a JSON line on its own stdout, which the host copies out and the sealed log alone keeps. It takes
calls from the job service alone, holding the node's token, and runs the job in an empty workspace. Before a
job with a credential on the forge starts, the daemon clones each company repo's ``main`` from the forge into
the workspace with that credential, so the job runs what is merged, as the job, and puts those repos on its
``PYTHONPATH`` unless the job brings its own, as the seed's jobs do; the job's start fact names the commit of
each. A job cannot change what another job runs. The job's process gets the job's own credentials the service
sends, its badge among them, which is its key to the inference gateway, and where the gateway is and the model
it serves jobs. It imports nothing of the harness.
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
GATEWAY_MODEL_VARIABLE = "MERIDIAN_GATEWAY_MODEL"  # the model a job calls it for
FORGE_TOKEN_VARIABLE = "MERIDIAN_FORGE_TOKEN"  # the job's own credential on the forge, as jobsvc names it
MAIN_CHECKOUT = "meridian"  # the workspace's directory of the repos' main, as the cluster's own /opt/meridian


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
        description="The inference gateway's API as a job calls it, on the runners' route, with its badge.",
    )
    gateway_model: str = Field(
        min_length=1,
        description="The model a job is given to call the gateway for: one the runners' route allows.",
    )
    forge_url: str | None = Field(
        description="The forge's address, http://forge:<port>, whose repos' main each job with a credential "
        "on it runs; none when the scenario has no forge.",
    )
    repos: tuple[str, ...] = Field(description="The company repos whose main a job's workspace holds.")


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
    ) -> None:
        """Run jobs in ``settings.workspace_directory`` and write each process fact with ``write_fact``."""
        self._settings = settings
        self._write_fact = write_fact
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
            token = job.environment.get(FORGE_TOKEN_VARIABLE)
            commits = self._check_out_main(workspace / MAIN_CHECKOUT, token)
            environment = {
                GATEWAY_URL_VARIABLE: self._settings.gateway_url,
                GATEWAY_MODEL_VARIABLE: self._settings.gateway_model,
                "PYTHONPATH": os.pathsep.join(str(workspace / MAIN_CHECKOUT / repo) for repo in commits),
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
        start = {"job_id": job.job_id, "owner": job.owner, "attempt": job.attempt, "argv": list(job.argv)}
        self._fact("start", **start, **({"checkout": commits} if commits else {}))

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

    def _check_out_main(self, checkout: Path, token: SecretStr | None) -> dict[str, str]:
        """Clone each repo's ``main`` from the forge into ``checkout`` with ``token``; each commit, by repo.

        Nothing without a forge or a token: the job imports none of the company's code then. A clone the forge
        refuses refuses the job (422), which the job service records as its failure.
        """
        if self._settings.forge_url is None or token is None:
            return {}
        header = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader"}
        git_environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            **header,
            "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {token.get_secret_value()}",
        }
        commits = {}
        for repo in self._settings.repos:
            url, destination = f"{self._settings.forge_url}/git/{repo}", checkout / repo
            cloned = subprocess.run(
                ["git", "clone", "--quiet", "--depth", "1", url, str(destination)],
                capture_output=True,
                text=True,
                check=False,
                env=git_environment,
            )
            if cloned.returncode != 0:
                detail = f"cannot check out {repo}'s main from the forge: {cloned.stderr.strip()}"
                raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, detail)
            head = ["git", "-C", str(destination), "rev-parse", "HEAD"]
            commits[repo] = subprocess.run(head, capture_output=True, text=True, check=True).stdout.strip()
        return commits

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
    process = NodeProcess(settings, lambda line: print(line, flush=True))
    # No access log: the job service polls every second, and the log is the node's record of process facts.
    app = create_node_app(settings, token, process)
    uvicorn.run(app, host=ALL_INTERFACES, port=settings.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
