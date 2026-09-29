"""A per-episode compose stack's lifecycle over ``docker compose``: build, up, one-off runs, teardown.

Enforces the sealed-vs-tamperable isolation structurally (the stack is rendered from
``configs/env.default.yaml`` by :mod:`loc_arena.compose_document`; nothing here opens a route the rendered
networks and mounts do not) and project isolation: every command names its project, so concurrent episodes
never touch each other's containers, networks, volumes or images. Each stack gets its own control key, written
to a file only this process knows the path of; every compose command of the stack is given that path (the
rendered control_key secret reads it), and teardown deletes it. Each stack builds and runs images of its own,
tagged with its project name, which every compose command is given the same way; teardown removes them.
Every container, network and volume is labelled ``loc-arena.eval=1``, so ``scripts/teardown.sh`` can sweep
them all by hand.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final

import yaml
from pydantic import SecretStr

from loc_arena.compose_document import render_compose
from loc_arena.config import RunConfig
from loc_arena.harness import PROJECT_DIRECTORY
from loc_arena.stack.constants import (
    CONTROL_KEY_BYTES,
    CONTROL_KEY_FILE_ENVIRONMENT_VARIABLE,
    CONTROL_KEY_SECRET_NAME,
    IMAGE_TAG_ENVIRONMENT_VARIABLE,
    IMAGE_TAG_MAX_LENGTH,
    RUNNER_OUTPUT_MOUNT_PATH,
)
from loc_arena.stack.settings import DockerSettings

RUNNER_SERVICE: Final = "runner"  # the on-demand service the runner phase runs in
# The control key file is bind-mounted into the core, the edge and the runner, and a bound file keeps its host
# owner and mode, so it must be readable by their users; its 0o700 directory keeps it from other local users.
CONTROL_KEY_FILE_MODE: Final = 0o444


class HarnessError(RuntimeError):
    """A docker/compose operation failed."""


def docker_available() -> bool:
    """True iff a docker daemon is reachable (integration tests skip-guard on this)."""
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@dataclass
class EpisodeStack:
    """A brought-up stack: its compose project, its rendered compose file, and its control key file.

    Every ``docker compose`` command of the project is given ``control_key_file`` (the rendered control_key
    secret reads its path), ``image_tag`` (every rendered service image reads it) and ``secret_environment``:
    the values of the compose secrets' ``environment:`` sources. They go only to the ``docker compose``
    process, never to a container; each secret value masks itself.
    """

    project: str
    compose_file: Path
    control_key_file: Path
    settings: DockerSettings
    secret_environment: dict[str, SecretStr] = field(default_factory=dict)

    @property
    def image_tag(self) -> str:
        """The tag of the images this stack builds and runs: its project name, which no other stack shares."""
        return self.project

    def exec(
        self,
        service: str,
        command: list[str],
        *,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command inside a service container (used by the isolation probes)."""
        return run_compose(self, ["exec", "-T", service, *command], check=check)

    def container_id(self, service: str) -> str:
        """The container id for a service in THIS project (project-scoped; safe with concurrent stacks)."""
        result = run_compose(self, ["ps", "-q", service], check=False)
        return result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""

    def running_services(self) -> set[str]:
        """The set of services currently running in this project."""
        result = run_compose(self, ["ps", "--services", "--status", "running"], check=False)
        return {s for s in result.stdout.split() if s}


def run_compose(
    stack: EpisodeStack,
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    timeout_seconds: float | None = None,
    stdout: IO[bytes] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``docker compose`` on the stack's project; ``stdout``, when given, receives its output."""
    command = [
        "docker",
        "compose",
        "-p",
        stack.project,
        "-f",
        str(stack.compose_file),
        "--project-directory",
        str(PROJECT_DIRECTORY),  # relative paths in the rendered file (./logs) resolve against the repo root
        *args,
    ]
    revealed_secrets = {
        variable: secret.get_secret_value() for variable, secret in stack.secret_environment.items()
    }
    environment = {
        **os.environ,
        **revealed_secrets,
        CONTROL_KEY_FILE_ENVIRONMENT_VARIABLE: str(stack.control_key_file),
        IMAGE_TAG_ENVIRONMENT_VARIABLE: stack.image_tag,
    }
    pipe = subprocess.PIPE if capture else None
    return subprocess.run(
        command,
        stdout=stdout if stdout is not None else pipe,
        stderr=pipe,
        text=True,
        check=check,
        env=environment,
        timeout=timeout_seconds,
    )


def run_compose_checked(stack: EpisodeStack, args: list[str], what: str) -> None:
    """Run ``docker compose`` and raise ``HarnessError`` (with the end of its stderr) when it fails."""
    result = run_compose(stack, args, check=False)
    if result.returncode != 0:
        tail = result.stderr[-stack.settings.error_output_characters :]
        raise HarnessError(f"{what} failed:\n{result.stdout}\n{tail}")


def write_control_key_file() -> Path:
    """A fresh control key, in a file of its own directory (0o700) that only the harness knows the path of."""
    directory = Path(tempfile.mkdtemp(prefix="locarena-key-"))  # mkdtemp creates it 0o700
    path = directory / CONTROL_KEY_SECRET_NAME
    path.write_text(secrets.token_hex(CONTROL_KEY_BYTES))
    path.chmod(CONTROL_KEY_FILE_MODE)
    return path


def up(
    config: RunConfig,
    *,
    project: str,
    workdir: Path | None = None,
    secret_environment: dict[str, SecretStr] | None = None,
) -> EpisodeStack:
    """Render the compose file, build its images (tagged for this stack alone), and bring it up healthy.

    A fresh control key is written for the stack. ``secret_environment`` supplies the environment-sourced
    compose secrets (the provider key for gateway_core); a secret it leaves out is empty, so the core holds no
    provider key: enough for the isolation tests and for scripted runs.
    """
    out_dir = workdir if workdir is not None else Path(tempfile.mkdtemp(prefix="locarena-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    compose_file = out_dir / "compose.resolved.yaml"
    compose_file.write_text(yaml.safe_dump(render_compose(config), sort_keys=False))
    empty_secrets = {variable: SecretStr("") for variable in _secret_source_variables(config)}
    stack = EpisodeStack(
        project=project,
        compose_file=compose_file,
        control_key_file=write_control_key_file(),
        settings=config.settings.docker,
        secret_environment={**empty_secrets, **(secret_environment or {})},
    )
    docker = config.settings.docker
    try:
        # Every profile, so the runner's and the grader's images (their build targets) are built as well.
        run_compose_checked(stack, ["--profile", "*", "build"], "building the stack's images")
        wait = ["up", "-d", "--wait", "--wait-timeout", str(docker.up_wait_timeout_seconds)]
        run_compose_checked(stack, wait, "bringing the stack up healthy")
    except BaseException:
        teardown(stack)
        raise
    return stack


def teardown(stack: EpisodeStack) -> None:
    """Remove every resource of THIS project (containers, networks, volumes, images) and its control key.

    Idempotent, and project-scoped so tearing one episode down never touches another concurrent episode (they
    share the ``loc-arena.eval`` label). ``--profile "*"`` includes on-demand services such as the runner.
    ``--rmi all`` removes the images the services name, which carry this stack's own tag (``local`` skips an
    image with a custom tag: docs.docker.com/reference/cli/docker/compose/down), so tags do not pile up. The
    next build still reuses the build cache, which only ``docker builder prune`` or BuildKit's garbage
    collection clears (docs.docker.com/build/cache/garbage-collection). ``scripts/teardown.sh`` is the manual
    sweep.
    """
    down_timeout = str(stack.settings.down_timeout_seconds)
    down = ["--profile", "*", "down", "-v", "--remove-orphans", "--rmi", "all", "-t", down_timeout]
    run_compose(stack, down, check=False)
    shutil.rmtree(stack.control_key_file.parent, ignore_errors=True)


def run_one_off(
    stack: EpisodeStack,
    service: str,
    arguments: list[str],
    *,
    capture: bool = True,
    timeout_seconds: float | None = None,
    stdout: IO[bytes] | None = None,
) -> subprocess.CompletedProcess[str]:
    """``docker compose run --rm`` one container of ``service``, killed if it outlives ``timeout_seconds``.

    The container is named, so that on expiry it can be removed: killing the compose client alone would leave
    the container running (and its volumes in use).
    """
    name = f"{stack.project}-{service}-{secrets.token_hex(3)}"
    run = ["run", "--rm", "-T", "--name", name, *arguments]
    try:
        return run_compose(
            stack,
            run,
            check=False,
            capture=capture,
            timeout_seconds=timeout_seconds,
            stdout=stdout,
        )
    except subprocess.TimeoutExpired as error:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True, check=False)
        raise HarnessError(
            f"{service} ran past its wall-clock limit of {timeout_seconds} s and was killed",
        ) from error


def run_in_runner(
    stack: EpisodeStack,
    command: list[str],
    *,
    output_directory: Path | None = None,
    capture: bool = True,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``command`` in a fresh runner container (removed afterwards) as the host user.

    ``output_directory`` (a host directory) is bind-mounted at ``/app/logs`` for this container only; running
    as the host uid keeps what the run writes there owned by that user. Past ``timeout_seconds`` the container
    is killed and ``HarnessError`` raised.
    """
    user = f"{os.getuid()}:{os.getgid()}"
    output_mount = (
        ["--volume", f"{output_directory.resolve()}:{RUNNER_OUTPUT_MOUNT_PATH.as_posix()}"]
        if output_directory
        else []
    )
    return run_one_off(
        stack,
        RUNNER_SERVICE,
        ["--user", user, *output_mount, RUNNER_SERVICE, *command],
        capture=capture,
        timeout_seconds=timeout_seconds,
    )


def collect_run_output(staging_directory: Path, logs_directory: Path) -> list[Path]:
    """Copy what a runner wrote into ``logs_directory`` and remove the staging directory.

    The runner (agent code included) controlled ``staging_directory``, so nothing in it is followed: only real
    directories and regular files are copied; symlinks, FIFOs, sockets and devices are dropped. Returns the
    dropped paths, relative to ``staging_directory``. Call it after the runner container is gone.
    """
    dropped: list[Path] = []
    for directory, subdirectory_names, file_names in os.walk(staging_directory, followlinks=False):
        source_directory = Path(directory)
        target_directory = logs_directory / source_directory.relative_to(staging_directory)
        target_directory.mkdir(parents=True, exist_ok=True)
        for name in list(subdirectory_names):
            if (source_directory / name).is_symlink():
                subdirectory_names.remove(name)  # os.walk does not descend into it
                dropped.append((source_directory / name).relative_to(staging_directory))
        for name in file_names:
            source = source_directory / name
            if stat.S_ISREG(source.lstat().st_mode):
                shutil.copy2(source, target_directory / name, follow_symlinks=False)
            else:
                dropped.append(source.relative_to(staging_directory))
    shutil.rmtree(staging_directory)
    return dropped


def _secret_source_variables(config: RunConfig) -> list[str]:
    """The environment variables the config's compose secrets read their values from."""
    return [
        str(spec["environment"]) for spec in config.raw.get("secrets", {}).values() if "environment" in spec
    ]


def name_compose_project(run: str) -> str:
    """A unique compose project name for one run, also a valid image tag (it is ``EpisodeStack.image_tag``).

    Lowercase letters, digits, hyphens and underscores only (compose's project name rule, a subset of what a
    tag allows), cut to the length a tag allows.
    """
    suffix = f"-{secrets.token_hex(3)}"
    run_fragment = re.sub(r"[^a-z0-9_-]+", "-", run.lower()).strip("-") or "run"
    return f"locarena-{run_fragment}"[: IMAGE_TAG_MAX_LENGTH - len(suffix)] + suffix
