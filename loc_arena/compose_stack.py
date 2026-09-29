"""A per-episode compose stack's lifecycle over ``docker compose``: build, up, one-off runs, teardown."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final

from pydantic import SecretStr

from loc_arena.compose_document import PROJECT_DIRECTORY, dump_compose_document, render_compose
from loc_arena.config import RunConfig
from loc_arena.stack.constants import (
    CONTROL_KEY_BYTES,
    CONTROL_KEY_FILE_ENVIRONMENT_VARIABLE,
    CONTROL_KEY_SECRET_NAME,
    IMAGE_TAG_ENVIRONMENT_VARIABLE,
    IMAGE_TAG_MAX_LENGTH,
    RUNNER_OUTPUT_MOUNT_PATH,
)
from loc_arena.stack.settings import DockerSettings

RUNNER_SERVICE: Final = "runner"
# A bind-mounted file keeps its host mode and the containers' users read it; its 0o700 directory guards it.
CONTROL_KEY_FILE_MODE: Final = 0o444


class HarnessError(RuntimeError):
    """A docker/compose operation failed."""


def docker_available(settings: DockerSettings | None = None) -> bool:
    """True iff a docker daemon answers ``docker info`` within ``settings.daemon_check_timeout_seconds``.

    The integration tests skip-guard on this, with the default settings.
    """
    if shutil.which("docker") is None:
        return False
    timeout_seconds = (settings or DockerSettings()).daemon_check_timeout_seconds
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:  # subprocess.run has killed the client
        return False
    return result.returncode == 0


@dataclass
class EpisodeStack:
    """A brought-up stack: its compose project, compose file, control key file and secret sources."""

    project: str
    compose_file: Path
    control_key_file: Path
    settings: DockerSettings
    secret_environment: dict[str, SecretStr] = field(default_factory=dict)  # for the compose process only

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
        str(PROJECT_DIRECTORY),  # the rendered file's relative binds (./scenarios/...) resolve against it
        *args,
    ]
    revealed_secrets = {
        variable: secret.get_secret_value() for variable, secret in stack.secret_environment.items()
    }
    environment = {
        **os.environ,
        **revealed_secrets,
        CONTROL_KEY_FILE_ENVIRONMENT_VARIABLE: str(stack.control_key_file),
        IMAGE_TAG_ENVIRONMENT_VARIABLE: stack.project,  # a stack's images are tagged with its project
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
    """Render the compose file, build its images under the stack's own tag, and bring it up healthy."""
    out_dir = workdir if workdir is not None else Path(tempfile.mkdtemp(prefix="locarena-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    compose_file = out_dir / "compose.resolved.yaml"
    document = render_compose(config)
    compose_file.write_text(dump_compose_document(document))
    sources = document.get("secrets", {}).values()
    empty_secrets = {source["environment"]: SecretStr("") for source in sources if "environment" in source}
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
    """Remove this project's containers, networks, volumes, images and control key file; idempotent."""
    down_timeout = str(stack.settings.down_timeout_seconds)
    down = ["--profile", "*", "down", "-v", "--remove-orphans", "--rmi", "all", "-t", down_timeout]
    result = run_compose(stack, down, check=False)
    shutil.rmtree(stack.control_key_file.parent, ignore_errors=True)
    if result.returncode != 0:
        tail = result.stderr[-stack.settings.error_output_characters :]
        print(
            f"tearing down {stack.project} failed, so its containers, networks, volumes or images may be "
            f"left; scripts/teardown.sh removes them:\n{tail}",
            file=sys.stderr,
        )


def run_one_off(
    stack: EpisodeStack,
    service: str,
    arguments: list[str],
    *,
    capture: bool = True,
    timeout_seconds: float | None = None,
    stdout: IO[bytes] | None = None,
) -> subprocess.CompletedProcess[str]:
    """``docker compose run --rm`` one container of ``service``, killed if it outlives ``timeout_seconds``."""
    name = f"{stack.project}-{service}-{secrets.token_hex(3)}"  # killing the client alone leaves it running
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
    """Run ``command`` in a fresh runner container as the host user, ``output_directory`` at /app/logs."""
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
    """Move a runner's real directories and regular files to ``logs_directory``; return what it dropped."""
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


def name_compose_project(run: str) -> str:
    """A unique compose project name for ``run``, also valid as the stack's image tag."""
    suffix = f"-{secrets.token_hex(3)}"
    run_fragment = re.sub(r"[^a-z0-9_-]+", "-", run.lower()).strip("-") or "run"
    return f"locarena-{run_fragment}"[: IMAGE_TAG_MAX_LENGTH - len(suffix)] + suffix
