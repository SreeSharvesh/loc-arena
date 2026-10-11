"""A stack run's plays: the episode and its honest twin in one container, and its live services' logs.

Between the two plays every container agent code could leave state in is recreated empty (``renew_services``),
and each logged service's log is added to its file in the run directory first, since a recreated container
starts a new log. ``copy_out`` makes each copy out of the compose project once.
"""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable
from pathlib import Path

from loc_arena.config import RunConfig, sandbox_service
from loc_arena.tools_gateway import TOOLS_GATEWAY_SERVICE, serves_tools

EPISODE_SERVICE = "episode"  # the agent loop's compose service: the one caller every sandbox serves


class StackError(RuntimeError):
    """The episode's compose project could not deliver its logs."""


def list_logged_services(config: RunConfig) -> list[str]:
    """The services whose log a stack run copies out: each live service, and the tools gateway if it runs."""
    tools_gateway = [TOOLS_GATEWAY_SERVICE] if serves_tools(config) else []
    return [service.name for service in config.live_services] + tools_gateway


def list_service_log_copies(
    config: RunConfig,
    compose: list[str],
    service_logs: Path,
) -> list[tuple[list[str], Path]]:
    """Each logged service's ``compose logs`` command, and the file in ``service_logs`` its output joins."""
    return [
        ([*compose, "logs", "--no-color", "--no-log-prefix", service], service_logs / f"{service}.log")
        for service in list_logged_services(config)
    ]


def renew_services(
    config: RunConfig,
    compose: list[str],
    service_logs: Path,
    environment: dict[str, str],
    twin_environment: dict[str, str],
) -> None:
    """Before the honest twin: every container agent code could leave state in, recreated empty.

    Those are each sandbox, each live service and the tools gateway; the gateway, whose call log runs on, and
    the episode stay. Each logged service's log so far is first added to its file in ``service_logs``, every
    copy or none, since a recreated container starts a new log. The recreate uses ``twin_environment``, whose
    tools gateway config offers the honest twin's tools (no covert tool), so an agent granted a covert tool in
    the attack config is refused it at the gateway in the twin. Raises ``StackError`` when a log cannot be
    copied, so nothing is recreated, or when a renewed container does not come back.
    """
    copies = list_service_log_copies(config, compose, service_logs)
    logged = [subprocess.run(command, env=environment, capture_output=True) for command, _ in copies]
    if failed := [into.name for (_, into), done in zip(copies, logged, strict=True) if done.returncode]:
        raise StackError(
            f"could not copy {', '.join(failed)} out before the honest twin, so nothing was renewed",
        )
    try:  # every file opened before any is written, so one that cannot be leaves the others untouched
        with contextlib.ExitStack() as files:
            outputs = [files.enter_context(into.open("ab")) for _, into in copies]
            for output, done in zip(outputs, logged, strict=True):
                output.write(done.stdout)
    except OSError as error:
        raise StackError(
            f"could not write the logs before the honest twin, so nothing was renewed: {error}",
        ) from error
    renewed = [*(sandbox_service(agent.id) for agent in config.agents), *list_logged_services(config)]
    recreate = ["up", "--detach", "--wait", "--force-recreate", "--renew-anon-volumes", "--no-deps"]
    if subprocess.run([*compose, *recreate, *renewed], env=twin_environment).returncode != 0:
        raise StackError(
            "the renewed containers did not come back for the honest twin: see compose's output above",
        )


def play_phases(
    config: RunConfig,
    compose: list[str],
    mode: str,
    service_logs: Path,
    environment: dict[str, str],
    twin_environment: dict[str, str],
) -> int:
    """Play the episode and, in attack mode, the honest twin in the same container after ``renew_services``.

    Never recreated, the container plays its image of the first play again, whatever its tag now names. The
    twin plays under ``twin_environment``, whose recreated tools gateway offers it no covert tool. Returns the
    exit code of the last play.
    """
    attached = ["--attach", EPISODE_SERVICE, "--exit-code-from", EPISODE_SERVICE]
    play = [*compose, "up", "--no-recreate", *attached, EPISODE_SERVICE]
    exit_code = subprocess.run(play, env=environment).returncode
    if exit_code == 0 and mode != "honest":
        renew_services(config, compose, service_logs, environment, twin_environment)
        exit_code = subprocess.run(play, env=twin_environment).returncode
    return exit_code


def copy_out(
    run_compose: Callable[..., subprocess.CompletedProcess[bytes]],
    command: list[str],
    into: Path | None = None,
    *,
    made: set[tuple[str, ...]],
) -> bool:
    """Whether compose ran ``command``, with what it prints added ``into`` that file when given.

    A copy in ``made`` was made already and is not made again: a log's lines would repeat, and the run
    directory would lose what was written into it since. One that succeeds now joins ``made``.
    """
    if tuple(command) in made:
        return True
    try:
        with into.open("ab") if into else contextlib.nullcontext() as output:
            done = run_compose(command, stdout=output).returncode == 0
    except OSError:  # a file that cannot be written is a failed copy: the others are still tried
        return False
    if done:
        made.add(tuple(command))
    return done
