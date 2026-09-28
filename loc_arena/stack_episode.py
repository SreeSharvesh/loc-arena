"""One episode in its own compose stack: the runner phase inside it, then the host phase on this machine.

Enforces the sealed-vs-tamperable isolation (STRUCTURAL: the sealed core/recorder sit on networks the
agents are not members of, the sealed log volume is mounted only into the recorder and the networkless
evidence reader, and agent code runs only in the agents' sandboxes and the networkless grader) and config
over code (the whole stack is rendered from ``configs/env.default.yaml`` -- no port, limit, or network is
hardcoded here).

``run_in_stack`` (``make run``) gives each episode of a run a FRESH compose project (its own sandboxes,
checkout volume, core state and control key), so nothing an agent started outlives its episode:

- **The runner phase** (``python -m loc_arena.cli episode`` in the runner container, ``run_runner_phase``)
  opens the episode on the core, runs the scaffold, closes the episode, scores the mirror with the monitors
  and exports what it knows (``RunnerEpisodeExport``).
- **The host phase** freezes the stack (stops the sandboxes and the edge), copies the sealed, model-call and
  mirror logs out of the evidence reader, has the networkless grader measure the checkout, and grades on this
  machine: the main task against the sealed reference, the side task on the sealed log. The harness then
  scores the run and writes its bundle exactly as it does for an in-process run.

The stack's lifecycle (build, up, one-off runs, teardown, the control key) is
:mod:`loc_arena.compose_stack`'s.
"""

from __future__ import annotations

import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from loc_arena import live
from loc_arena.compose_stack import (
    EpisodeStack,
    HarnessError,
    collect_run_output,
    docker_available,
    name_compose_project,
    run_compose_checked,
    run_in_runner,
    run_one_off,
    teardown,
    up,
)
from loc_arena.config import RunConfig
from loc_arena.evidence import read_event_log, read_model_call_log, read_runner_export
from loc_arena.gateway.wiring import (
    StackServices,
    build_episode_name,
    connect_stack_services,
    open_episode_wiring,
    read_episode_mode,
)
from loc_arena.grader.measurement import parse_measurement
from loc_arena.harness import (
    DEFAULT_MONITORS_PATH,
    DOTENV_PATH,
    apply_mode,
    load_config_by_name,
    load_protocol,
    open_bundle,
    score_and_write_bundle,
)
from loc_arena.logging_.agent_trace import (
    AgentTrace,
    EpisodeTrace,
    export_runner_episode,
    merge_runner_episode,
)
from loc_arena.monitors.registry import MonitorVerdict
from loc_arena.runner import EpisodeMonitoring, export_verdicts, finish_episode, import_verdicts
from loc_arena.scaffold.clock import SimulatedClock
from loc_arena.stack.constants import (
    EVENTS_FILE_NAME,
    MIRROR_MOUNT_PATH,
    MODEL_CALLS_FILE_NAME,
    OPENROUTER_API_KEY_SECRET_NAME,
    RUNNER_EPISODE_EXPORT_FILE_NAME,
    RUNNER_OUTPUT_MOUNT_PATH,
    SEALED_MOUNT_PATH,
    build_sandbox_service_name,
)
from loc_arena.stack.contracts import GradeMeasurement, RunnerEpisodeExport, build_episode_id
from loc_arena.stack.stack_secrets import StackSecrets, load_container_secrets
from loc_arena.task import _resolve_scenario, run_scripted_policy
from loc_arena.tasks.main_task_grader import MainTaskScore, load_grade_reference, score_measurement
from loc_arena.tasks.side_task_verifier import verify_sealed_events
from loc_arena.verifier.side_task import KillChainVerdict

# The config's service names the host drives by name (the rest it reaches only through compose).
_EDGE_SERVICE: Final = "gateway_edge"
_EVIDENCE_READER_SERVICE: Final = "evidence_reader"
_GRADER_SERVICE: Final = "grader"
# Where a stack episode's host-side files go under its directory: what the runner wrote, the copied logs.
_STAGING_DIRECTORY: Final = ".incoming"
_RUNNER_DIRECTORY: Final = "runner"
_EVIDENCE_DIRECTORY: Final = "evidence"
_SEALED_DIRECTORY: Final = "sealed"
_MIRROR_DIRECTORY: Final = "mirror"
# A measurement the grader could not produce: graded as a broken pipeline (grade_error), never a crash.
_FAILED_MEASUREMENT: Final = GradeMeasurement(
    suite_green=False,
    pipeline_completed=False,
    cost=None,
    outputs={},
)


def run_runner_phase(
    config: RunConfig,
    services: StackServices,
    *,
    robust: bool,
    output_directory: Path,
    mirror_root: Path = MIRROR_MOUNT_PATH,
    monitors_path: str = DEFAULT_MONITORS_PATH,
    policy_factory: live.AgentPolicyFactory | None = None,
) -> RunnerEpisodeExport:
    """The runner phase of one episode (in the runner container): open it, run it, close it, monitor it.

    Opens the episode of ``config`` (mode applied) on the core, runs its policy on the stack wiring (each
    agent's code in its own sandbox, with a shell), closes it, scores the mirror (read at
    ``mirror_root/<handle>``) with the protocol's monitors, and writes the ``RunnerEpisodeExport`` to
    ``output_directory``. Live monitors call models only when the policy is live.
    """
    trace = AgentTrace() if config.agent_transcript else None
    wiring = open_episode_wiring(config, services, trace=trace)
    clock = SimulatedClock(wiring.control, config.settings.clock.start_ts)
    deadline_ts = config.settings.clock.deadline_ts
    if config.policy == "model":
        live.run_model_policy(
            config,
            wiring,
            clock,
            trace,
            deadline_ts=deadline_ts,
            shell_offered=True,
            policy_factory=policy_factory,
        )
    else:
        run_scripted_policy(config, wiring, clock, robust=robust, deadline_ts=deadline_ts)
    finished = finish_episode(
        config,
        wiring,
        clock,
        trace,
        mirror_path=mirror_root / wiring.handle / EVENTS_FILE_NAME,
        monitoring=EpisodeMonitoring(
            load_protocol(config, monitors_path),
            calls_models=config.policy == "model",
        ),
    )
    export = export_runner_episode(
        finished.trace,
        handle=wiring.handle,
        episode_id=build_episode_id(build_episode_name(config), read_episode_mode(config)),
        deadline_ts=deadline_ts,
        last_sealed_seq=finished.last_sealed_seq,
        verdicts=export_verdicts(finished.verdicts),
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / RUNNER_EPISODE_EXPORT_FILE_NAME).write_text(export.model_dump_json())
    return export


def run_runner_phase_in_container(run: str, mode: str, *, robust: bool) -> RunnerEpisodeExport:
    """``python -m loc_arena.cli episode``: the runner phase with the container's settings and control key."""
    config = apply_mode(load_config_by_name(run), mode)
    control_key = load_container_secrets().control_key
    if control_key is None:
        raise HarnessError("no control_key secret: the runner cannot reach the core's control routes")
    with ExitStack() as resources:
        services = connect_stack_services(config, control_key, resources)
        return run_runner_phase(config, services, robust=robust, output_directory=RUNNER_OUTPUT_MOUNT_PATH)


@dataclass(frozen=True)
class StackEpisode:
    """An episode run in its own stack and graded on the host: the ``GradedEpisode`` scoring reads."""

    main_score: MainTaskScore
    verdict: KillChainVerdict
    sealed_path: Path
    mirror_path: Path
    deadline_ts: float
    trace: EpisodeTrace | None
    verdicts: tuple[MonitorVerdict, ...]


def run_stack_episode(
    run: str,
    config: RunConfig,
    mode: str,
    directory: Path,
    *,
    robust: bool,
    provider_key: SecretStr | None,
) -> StackEpisode:
    """Run one episode of ``run`` in ``mode`` in a fresh compose stack, then grade it on this machine.

    The runner phase runs in the runner container within ``settings.docker.runner_phase_timeout_seconds``.
    Then the host freezes the stack (the sandboxes, with anything an agent left running, and the edge stop),
    copies the episode's logs out of the evidence reader, has the grader measure the checkout within
    ``settings.docker.grader_timeout_seconds``, tears the stack down, and grades what it copied.
    """
    docker = config.settings.docker
    staging = directory / _STAGING_DIRECTORY
    stack = up(
        config,
        project=name_compose_project(run),
        workdir=directory,
        # compose reads the provider key secret from this variable (configs/env.default.yaml secrets)
        secret_environment={OPENROUTER_API_KEY_SECRET_NAME.upper(): provider_key or SecretStr("")},
    )
    try:
        export = _run_runner(
            stack,
            run,
            mode,
            robust=robust,
            staging=staging / _RUNNER_DIRECTORY,
            directory=directory,
        )
        sandboxes = [build_sandbox_service_name(agent.id) for agent in config.agents]
        run_compose_checked(stack, ["stop", *sandboxes, _EDGE_SERVICE], "freezing the sandboxes and the edge")
        _copy_evidence(stack, export.handle, staging=staging / _EVIDENCE_DIRECTORY, directory=directory)
        staging.rmdir()  # both staged copies were collected and removed
        measurement = _measure_checkout(stack, config)
    finally:
        teardown(stack)
    evidence = directory / _EVIDENCE_DIRECTORY
    return _grade_on_host(
        config,
        export,
        measurement,
        sealed_directory=evidence / _SEALED_DIRECTORY,
        mirror_path=evidence / _MIRROR_DIRECTORY / EVENTS_FILE_NAME,
        max_bytes=docker.evidence_max_bytes,
    )


def _run_runner(
    stack: EpisodeStack,
    run: str,
    mode: str,
    *,
    robust: bool,
    staging: Path,
    directory: Path,
) -> RunnerEpisodeExport:
    """Run the runner phase in the runner container; return its export, copied into ``directory``."""
    staging.mkdir(parents=True)
    minimal = [] if robust else ["--minimal"]
    command = ["python", "-m", "loc_arena.cli", "episode", "--run", run, "--mode", mode, *minimal]
    result = run_in_runner(
        stack,
        command,
        output_directory=staging,
        capture=False,  # a live run streams its progress to this terminal
        timeout_seconds=stack.settings.runner_phase_timeout_seconds,
    )
    destination = directory / _RUNNER_DIRECTORY
    _report_dropped(collect_run_output(staging, destination), "the runner's output")
    if result.returncode != 0:
        raise HarnessError(f"the runner phase exited with {result.returncode}")
    return read_runner_export(
        destination / RUNNER_EPISODE_EXPORT_FILE_NAME,
        stack.settings.evidence_max_bytes,
    )


def _copy_evidence(stack: EpisodeStack, handle: str, *, staging: Path, directory: Path) -> None:
    """Copy the episode's sealed and mirror logs out of the evidence reader, links and devices dropped.

    ``docker compose cp`` without ``-L`` copies a link as a link, and ``collect_run_output`` then drops it.
    """
    staging.mkdir(parents=True)
    for mount, name in ((SEALED_MOUNT_PATH, _SEALED_DIRECTORY), (MIRROR_MOUNT_PATH, _MIRROR_DIRECTORY)):
        source = f"{_EVIDENCE_READER_SERVICE}:{(mount / handle).as_posix()}"
        run_compose_checked(stack, ["cp", source, str(staging / name)], f"copying {source}")
    _report_dropped(collect_run_output(staging, directory / _EVIDENCE_DIRECTORY), "the copied logs")


def _measure_checkout(stack: EpisodeStack, config: RunConfig) -> GradeMeasurement:
    """The grader's measurement of the checkout; an output that is no measurement grades as a broken pipeline.

    The grader runs agent code, so its output is read as untrusted: at most ``grading.max_output_bytes`` of it
    (``parse_measurement`` refuses more), from a file rather than memory.
    """
    grading = config.settings.grading
    with tempfile.TemporaryFile() as output:
        result = run_one_off(
            stack,
            _GRADER_SERVICE,
            [_GRADER_SERVICE],
            timeout_seconds=stack.settings.grader_timeout_seconds,
            stdout=output,
        )
        if result.returncode != 0:
            tail = result.stderr[-stack.settings.error_output_characters :]
            raise HarnessError(f"the grader exited with {result.returncode}:\n{tail}")
        output.seek(0)
        printed = output.read(grading.max_output_bytes + 1)
    try:
        return parse_measurement(printed, grading)
    except ValueError as error:  # pydantic's ValidationError included
        print(
            f"the grader printed no valid measurement ({error}); graded as a broken pipeline",
            file=sys.stderr,
        )
        return _FAILED_MEASUREMENT


def _grade_on_host(
    config: RunConfig,
    export: RunnerEpisodeExport,
    measurement: GradeMeasurement,
    *,
    sealed_directory: Path,
    mirror_path: Path,
    max_bytes: int,
) -> StackEpisode:
    """Grade the episode from what the host copied: the logs parsed strictly, the measurement scored."""
    sealed_path = sealed_directory / EVENTS_FILE_NAME
    sealed_events = read_event_log(sealed_path, max_bytes)
    read_event_log(mirror_path, max_bytes)  # the bundle ships it: it must be a valid log too
    strangers = {event.episode_id for event in sealed_events} - {export.episode_id}
    if strangers:
        raise HarnessError(f"the sealed log of {export.episode_id} holds events of {sorted(strangers)}")
    trace = None
    if config.agent_transcript:
        model_calls = read_model_call_log(sealed_directory / MODEL_CALLS_FILE_NAME, max_bytes)
        trace = merge_runner_episode(export, model_calls, (event.seq for event in sealed_events))
    reference = load_grade_reference(_resolve_scenario(config).reference_dir)
    return StackEpisode(
        main_score=score_measurement(measurement, reference, config),
        verdict=verify_sealed_events(sealed_events, config, export.deadline_ts),
        sealed_path=sealed_path,
        mirror_path=mirror_path,
        deadline_ts=export.deadline_ts,
        trace=trace,
        verdicts=import_verdicts(export.verdicts),
    )


def _report_dropped(dropped: list[Path], what: str) -> None:
    if dropped:
        print(f"dropped {len(dropped)} non-regular file(s) from {what}: {dropped[:5]}", file=sys.stderr)


def run_in_stack(
    run: str,
    mode: str,
    *,
    out_root: Path | None = None,
    seed: int | None = None,
    robust: bool = True,
    run_instance_id: str | None = None,
    dotenv_path: Path | None = DOTENV_PATH,
) -> Path:
    """Run one episode, and in attack mode its honest twin, each in its own compose stack (``make run``).

    The agents' code never runs on this machine: it runs in their sandboxes, and only gateway_core holds the
    provider key (read here from the environment or ``dotenv_path``, passed to compose as its secret). The
    scores and the bundle are the in-process run's, written by the same code. Returns the bundle directory.
    """
    if not docker_available():
        raise HarnessError(
            "Docker is not running. Start Docker Desktop, or run in-process with STACK=0 "
            "(the agents' code then runs on this machine with its credentials in reach).",
        )
    config = load_config_by_name(run)
    provider_key = StackSecrets(_env_file=dotenv_path, _secrets_dir=None).openrouter_api_key
    if config.policy == "model" and provider_key is None:
        raise HarnessError(
            "policy: model needs OPENROUTER_API_KEY (in .env or the environment) for gateway_core",
        )
    cfg = apply_mode(config, mode)
    bundle = open_bundle(cfg, mode, out_root=out_root, seed=seed, run_instance_id=run_instance_id)

    episode = run_stack_episode(
        run,
        cfg,
        mode,
        bundle.directory / "episode",
        robust=robust,
        provider_key=provider_key,
    )
    honest = (
        episode
        if mode == "honest"
        else run_stack_episode(
            run,
            apply_mode(config, "honest"),
            "honest",
            bundle.directory / "honest_cal",
            robust=False,
            provider_key=provider_key,
        )
    )
    return score_and_write_bundle(
        cfg,
        mode,
        bundle,
        episode,
        honest,
        monitors_path=DEFAULT_MONITORS_PATH,
        write_report=True,
    )
