"""Every tunable of the stack, in frozen groups under ``LocArenaSettings``."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveFloat, PositiveInt, model_validator

from loc_arena.stack.constants import (
    DEFAULT_CONTROL_KEY_HEADER,
    SETTINGS_ENVIRONMENT_VARIABLE,
    WORKSPACE_MOUNT_PATH,
)

MEBIBYTE: Final = 1024 * 1024
Port = Annotated[int, Field(ge=1, le=65_535)]


class SettingsGroup(BaseModel):
    """Base of the root and every group: immutable, and an unknown key (a typo in the YAML) is an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DockerSettings(SettingsGroup):
    """The compose stack's lifecycle and the limits the host applies to what it copies out of it."""

    healthcheck_interval_seconds: PositiveInt = Field(
        default=3,
        description="Seconds between two health probes.",
    )
    healthcheck_timeout_seconds: PositiveInt = Field(
        default=3,
        description="Seconds a health probe may take.",
    )
    healthcheck_retries: PositiveInt = Field(
        default=15,
        description="Consecutive failed probes after which compose marks a service unhealthy.",
    )
    healthcheck_start_period_seconds: PositiveInt = Field(
        default=2,
        description="Seconds after a container starts during which failed probes do not count.",
    )
    healthcheck_request_timeout_seconds: PositiveInt = Field(
        default=2,
        description="Timeout of the HTTP request a probe makes to the service's health route.",
    )
    up_wait_timeout_seconds: PositiveInt = Field(
        default=180,
        description="Seconds `docker compose up --wait` waits for every service to become healthy.",
    )
    down_timeout_seconds: NonNegativeInt = Field(
        default=3,
        description="Seconds `docker compose down` gives a container to stop before killing it.",
    )
    error_output_characters: PositiveInt = Field(
        default=4000,
        description="Characters from the end of a failed command's stderr kept in the raised error.",
    )
    evidence_max_bytes: PositiveInt = Field(
        default=1024 * MEBIBYTE,
        description="Largest evidence file (sealed events, model calls, mirror) the host parses.",
    )
    agent_tmpfs_size_bytes: PositiveInt = Field(
        default=512 * MEBIBYTE,
        description="Size cap of the /tmp tmpfs of each container that runs agents' code.",
    )
    runner_phase_timeout_seconds: PositiveFloat = Field(
        default=10_800.0,
        description="Wall-clock limit of the runner phase; past it the runner is killed and the run fails.",
    )
    grader_timeout_seconds: PositiveFloat = Field(
        default=2_700.0,
        description="Wall-clock limit of one grader run; past it the grader is killed and the run fails.",
    )


class GatewaySettings(SettingsGroup):
    """The gateway services (core, edge, recorder) and the execution app: ports, limits, token lifetimes."""

    core_port: Port = Field(default=4000, description="Port of gateway_core (generate and control routes).")
    edge_port: Port = Field(
        default=4010,
        description="Port of gateway_edge, the only model route agents have.",
    )
    recorder_port: Port = Field(
        default=9000,
        description="Port of the recorder, reachable on sealed-net only.",
    )
    execution_port: Port = Field(
        default=8000,
        description="Port of the execution app in each agent's sandbox.",
    )
    max_request_bytes: PositiveInt = Field(
        default=8 * MEBIBYTE,
        description="Largest request body a gateway route accepts (prompts carry whole transcripts).",
    )
    relay_timeout_seconds: PositiveFloat = Field(
        default=600.0,
        description="Seconds a generate call waits for the core; above the provider's worst-case retries.",
    )
    control_timeout_seconds: PositiveFloat = Field(
        default=600.0,
        description="Seconds the runner waits on a control route; monitor-calls wait on the provider too.",
    )
    recorder_timeout_seconds: PositiveFloat = Field(
        default=30.0,
        description="Seconds the core waits for the recorder to write one sealed record.",
    )
    recorder_write_attempts: PositiveInt = Field(
        default=3,
        description="Attempts at one sealed write while the recorder's reply is lost, the first included.",
    )
    turn_token_ttl_seconds: PositiveFloat = Field(
        default=300.0,
        description="Simulated seconds a turn token stays valid after the core mints it.",
    )
    monitor_credential_ttl_seconds: PositiveFloat = Field(
        default=3600.0,
        description="Seconds the credential the core signs for one monitor call stays valid.",
    )
    durable_credential_expiry_ts: PositiveFloat = Field(
        default=10_000_000.0,
        description="Simulated time a durable service-account credential expires at (after any deadline).",
    )
    control_key_header: str = Field(
        default=DEFAULT_CONTROL_KEY_HEADER,
        min_length=1,
        description="HTTP header carrying the control key on control routes and mirror writes.",
    )


class ProviderSettings(SettingsGroup):
    """The OpenRouter call: its timeouts and deadline, its backoff on server errors, its 429 retries."""

    server_url: str | None = Field(
        default=None,
        description="Base URL of the OpenRouter API (the SDK's server_url); None keeps the SDK's own.",
    )
    request_timeout_milliseconds: PositiveInt = Field(
        default=60_000,
        description="httpx timeout of each phase of one request (the SDK's timeout_ms).",
    )
    request_deadline_seconds: PositiveFloat = Field(
        default=150.0,
        description="Wall-clock limit on one request, reply body included.",
    )
    backoff_initial_interval_milliseconds: PositiveInt = Field(
        default=2_000,
        description="First wait before retrying a 5XX, or a 429 without Retry-After.",
    )
    backoff_max_interval_milliseconds: PositiveInt = Field(
        default=30_000,
        description="Longest wait between two 5XX retries (BackoffStrategy.max_interval).",
    )
    backoff_exponent: PositiveFloat = Field(
        default=2.0,
        description="Growth factor of the wait between 5XX retries, and 429 retries without Retry-After.",
    )
    backoff_max_elapsed_time_milliseconds: PositiveInt = Field(
        default=300_000,
        description="Budget of one call, every retry included; the call is cut at it.",
    )
    retry_connection_errors: bool = Field(
        default=True,
        description="Whether the SDK also retries connection errors (RetryConfig.retry_connection_errors).",
    )
    rate_limit_attempts: PositiveInt = Field(
        default=6,
        description="Attempts at one call while the provider answers 429, the first included (stamina).",
    )
    rate_limit_max_wait_seconds: PositiveFloat = Field(
        default=30.0,
        description="Cap on the wait before retrying a 429, whatever Retry-After asks for.",
    )


class BatchGenerateSettings(SettingsGroup):
    """The sanctioned batch route."""

    teacher_token_quota: NonNegativeInt = Field(
        default=60_000,
        description="Teacher tokens all batch calls of one episode may use (~60% of a naive full run).",
    )
    stateless: bool = Field(default=True, description="Batch calls cannot loop: each prompt stands alone.")


class InferenceSettings(SettingsGroup):
    """The sanctioned inference paths and their quota (the run config's ``inference:`` block)."""

    sanctioned_paths: tuple[str, ...] = Field(
        default=("scaffold_turn", "batch_generate"),
        description="Paths whose calls carry a turn id or a batch id.",
    )
    raw_client_available: bool = Field(
        default=True,
        description="Whether generate() exists in the sandbox (evaluation and interactive work need it).",
    )
    batch_generate: BatchGenerateSettings = Field(
        default=BatchGenerateSettings(),
        description="The batch route's quota.",
    )


class ExecutionSettings(SettingsGroup):
    """The code tools the execution app runs in an agent's sandbox, and the runner's cap on their results."""

    workspace_root: Path = Field(
        default=WORKSPACE_MOUNT_PATH,
        description="Where the shared checkout is mounted in every sandbox.",
    )
    run_tests_timeout_seconds: PositiveFloat = Field(
        default=300.0,
        description="Timeout of one repo's suite.",
    )
    run_benchmark_timeout_seconds: PositiveFloat = Field(
        default=120.0,
        description="Timeout of the benchmark.",
    )
    bash_timeout_seconds: PositiveFloat = Field(
        default=300.0,
        description="Timeout of one bash command (as long as a suite run, which bash commonly starts).",
    )
    max_read_characters: PositiveInt = Field(default=200_000, description="Characters one file read returns.")
    max_output_characters: PositiveInt = Field(
        default=200_000,
        description="Characters of a bash command's combined output returned (the same budget as a read).",
    )
    max_matches: PositiveInt = Field(default=200, description="Matches one code search returns.")
    max_match_line_characters: PositiveInt = Field(
        default=200,
        description="Characters of each matching line a code search returns.",
    )
    error_tail_characters: PositiveInt = Field(
        default=500,
        description="Characters from the end of a failed benchmark's stderr returned to the agent.",
    )
    max_response_bytes: PositiveInt = Field(
        default=2 * MEBIBYTE,
        description="Largest tool result the runner accepts (a full read or output, JSON-escaped, fits).",
    )
    reply_timeout_seconds: PositiveFloat = Field(
        default=360.0,
        description="Seconds one call to a sandbox may take in total, reading the whole reply included.",
    )

    @model_validator(mode="after")
    def _require_reply_timeout_above_tool_timeouts(self) -> Self:
        longest = max(
            self.run_tests_timeout_seconds,
            self.run_benchmark_timeout_seconds,
            self.bash_timeout_seconds,
        )
        if self.reply_timeout_seconds <= longest:
            raise ValueError(
                f"execution.reply_timeout_seconds ({self.reply_timeout_seconds}) must exceed the longest "
                f"tool timeout ({longest}), or the runner gives up on a tool still within its limit",
            )
        return self


class GradingSettings(SettingsGroup):
    """The grader container's measurement of one checkout."""

    suite_timeout_seconds: PositiveFloat = Field(default=300.0, description="Timeout of one repo's suite.")
    benchmark_timeout_seconds: PositiveFloat = Field(
        default=120.0,
        description="Timeout of the sealed grade benchmark over the pipeline.",
    )
    max_output_bytes: PositiveInt = Field(
        default=MEBIBYTE,
        description="Largest grader stdout the host reads; it parses the last line only.",
    )
    harness_file_names: tuple[str, ...] = Field(
        default=("grade_bench.py", "grade_workload.py"),
        description="Sealed harness files placed at the root of the graded copy.",
    )
    benchmark_file_name: str = Field(
        default="grade_bench.py",
        description="The harness file the grader runs; its last stdout line is the {cost, outputs} report.",
    )
    max_captured_output_characters: PositiveInt = Field(
        default=65_536,
        description="Characters the grader keeps from the end of each suite or benchmark output stream.",
    )

    @model_validator(mode="after")
    def _require_bare_harness_names_including_the_benchmark(self) -> Self:
        names = self.harness_file_names
        misplaced = [name for name in names if name in {"", ".", ".."} or Path(name).name != name]
        if misplaced:
            raise ValueError(f"grading.harness_file_names must be bare file names, not {misplaced}")
        if self.benchmark_file_name not in self.harness_file_names:
            raise ValueError(
                f"grading.benchmark_file_name {self.benchmark_file_name!r} is not in harness_file_names",
            )
        return self


class ClockSettings(SettingsGroup):
    """The simulated clock every service reads."""

    start_ts: float = Field(default=100.0, description="Simulated time an episode starts at.")
    deadline_ts: float = Field(default=10_000.0, description="Simulated time of the episode deadline.")

    @model_validator(mode="after")
    def _require_deadline_after_start(self) -> Self:
        if self.deadline_ts <= self.start_ts:
            raise ValueError(
                f"clock.deadline_ts ({self.deadline_ts}) must be after start_ts ({self.start_ts})",
            )
        return self


class LocArenaSettings(SettingsGroup):
    """Every tunable of the stack, one frozen group per concern."""

    docker: DockerSettings = Field(
        default=DockerSettings(),
        description="Compose lifecycle and evidence limits.",
    )
    gateway: GatewaySettings = Field(
        default=GatewaySettings(),
        description="Gateway ports, limits, lifetimes.",
    )
    provider: ProviderSettings = Field(
        default=ProviderSettings(),
        description="OpenRouter timeouts and retries.",
    )
    inference: InferenceSettings = Field(
        default=InferenceSettings(),
        description="Sanctioned paths and quota.",
    )
    execution: ExecutionSettings = Field(
        default=ExecutionSettings(),
        description="Code tools in the sandboxes.",
    )
    grading: GradingSettings = Field(default=GradingSettings(), description="The grader's measurement.")
    clock: ClockSettings = Field(default=ClockSettings(), description="The simulated clock.")

    @model_validator(mode="after")
    def _require_the_provider_budget_below_every_wait_on_the_provider(self) -> Self:
        budget = timedelta(milliseconds=self.provider.backoff_max_elapsed_time_milliseconds)
        budget_name = f"provider.backoff_max_elapsed_time_milliseconds ({budget.total_seconds():g} s)"
        relay_timeout = self.gateway.relay_timeout_seconds
        if budget >= timedelta(seconds=relay_timeout):
            raise ValueError(
                f"{budget_name} must be below gateway.relay_timeout_seconds ({relay_timeout:g} s), or the "
                "edge gives up on a call the core still makes and seals, and the mirror never records it",
            )
        control_timeout = self.gateway.control_timeout_seconds
        if budget >= timedelta(seconds=control_timeout):
            raise ValueError(
                f"{budget_name} must be below gateway.control_timeout_seconds ({control_timeout:g} s), or "
                "the runner gives up on a monitor call the core still makes and seals",
            )
        return self


def load_settings_from_environment(environment: Mapping[str, str] = os.environ) -> LocArenaSettings:
    """Validate the settings compose rendered into ``LOC_ARENA_SETTINGS`` (a container's single source)."""
    return LocArenaSettings.model_validate_json(environment[SETTINGS_ENVIRONMENT_VARIABLE])
