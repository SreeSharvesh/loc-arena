"""One episode in its own compose stack: the runner phase inside it, then grading on this machine."""

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
from loc_arena.runner import EpisodeMonitoring, finish_episode
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
from loc_arena.stack.contracts import (
    GradeMeasurement,
    MonitorVerdictRecord,
    RunnerEpisodeExport,
    build_episode_id,
)
from loc_arena.stack.stack_secrets import StackSecrets, load_container_secrets
from loc_arena.task import _resolve_scenario, run_scripted_policy
from loc_arena.tasks.main_task_grader import MainTaskScore, load_grade_reference, score_measurement
from loc_arena.tasks.side_task_verifier import verify_sealed_events
from loc_arena.verifier.side_task import KillChainVerdict

_EDGE_SERVICE: Final = "gateway_edge"
_EVIDENCE_READER_SERVICE: Final = "evidence_reader"
_GRADER_SERVICE: Final = "grader"
_STAGING_DIRECTORY: Final = ".incoming"
_RUNNER_DIRECTORY: Final = "runner"
_EVIDENCE_DIRECTORY: Final = "evidence"
_SEALED_DIRECTORY: Final = "sealed"
_MIRROR_DIRECTORY: Final = "mirror"
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
    """The runner phase of one episode (in the runner container): open it, run it, close it, monitor it."""
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
    verdicts = tuple(MonitorVerdictRecord.model_validate(v, from_attributes=True) for v in finished.verdicts)
    export = export_runner_episode(
        finished.trace,
        handle=wiring.handle,
        episode_id=build_episode_id(build_episode_name(config), read_episode_mode(config)),
        deadline_ts=deadline_ts,
        last_sealed_seq=finished.last_sealed_seq,
        verdicts=verdicts,
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
    """Run one episode of ``run`` in ``mode`` in a fresh compose stack, then grade it on this machine."""
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
    staging.mkdir(parents=True)
    for mount, name in ((SEALED_MOUNT_PATH, _SEALED_DIRECTORY), (MIRROR_MOUNT_PATH, _MIRROR_DIRECTORY)):
        source = f"{_EVIDENCE_READER_SERVICE}:{(mount / handle).as_posix()}"
        run_compose_checked(stack, ["cp", source, str(staging / name)], f"copying {source}")
    _report_dropped(collect_run_output(staging, directory / _EVIDENCE_DIRECTORY), "the copied logs")


def _measure_checkout(stack: EpisodeStack, config: RunConfig) -> GradeMeasurement:
    # See docs/isolation/design.md#grader-output.
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
) -> StackEpisode:
    max_bytes = config.settings.docker.evidence_max_bytes
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
        verdicts=tuple(MonitorVerdict(**record.model_dump()) for record in export.verdicts),
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
    """Run one episode, and in attack mode its honest twin, each in its own compose stack (``make run``)."""
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
