"""The tunable values the code reads, in frozen groups under ``LocArenaSettings``.

Each group is a top-level block of the run config (``inference:``). ``load_run_config`` validates the blocks
it finds into ``RunConfig.settings``; a group the config leaves out takes the defaults below.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    field_validator,
)


class SettingsGroup(BaseModel):
    """Base of the root and every group: immutable, and an unknown key (a typo in the YAML) is an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


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


class GatewaySettings(SettingsGroup):
    """The gateway container (``gateway:``): its one upstream, what it forwards, where it records calls."""

    upstream: HttpUrl = Field(
        default=HttpUrl("https://openrouter.ai"),
        description="The one upstream every request goes to; HTTPS only, since requests carry the key.",
    )
    allowed_paths: tuple[str, ...] = Field(
        default=("api/v1/chat/completions", "api/v1/completions", "api/v1/models"),
        description="Upstream paths a caller may reach; a request on any other path is refused and recorded.",
    )
    port: int = Field(default=8080, ge=1, le=65535, description="The port the gateway listens on.")
    timeout_seconds: PositiveFloat = Field(default=120.0, description="Upstream timeout of one request.")
    call_log: Path = Field(
        default=Path("/sealed/calls.jsonl"),
        description="The sealed call log, in a volume only the gateway mounts.",
    )
    secrets_dir: Path = Field(
        default=Path("/run/secrets"),
        description="Where compose mounts a container's secrets: the gateway's key (the file "
        "openrouter_api_key), and the sandbox token of the episode and the sandbox (sandbox_token).",
    )

    @field_validator("upstream")
    @classmethod
    def require_https(cls, upstream: HttpUrl) -> HttpUrl:
        """Refuse an upstream the key would reach in cleartext."""
        if upstream.scheme != "https":
            raise ValueError(f"the upstream must use https, not {upstream.scheme}")
        return upstream


class StackSettings(SettingsGroup):
    """The episode's compose project (``stack:``): its image and the episode container's limits."""

    image: str = Field(default="loc-arena:latest", description="The image every container runs.")
    episode_memory_limit: str = Field(default="4g", description="Memory limit of the episode container.")
    episode_cpus: PositiveFloat = Field(default=2.0, description="CPUs the episode container may use.")
    gateway_health_interval_seconds: PositiveInt = Field(
        default=2,
        description="Seconds between health probes of the gateway and of the sandbox.",
    )
    gateway_health_retries: PositiveInt = Field(
        default=15,
        description="Failed probes of the gateway or the sandbox before giving up.",
    )
    episode_pids_limit: PositiveInt = Field(default=4096, description="Processes the episode may run.")
    agent_code_output_limit_bytes: PositiveInt = Field(
        default=1_000_000,
        description="Bytes of an agent-code run's stdout and of its stderr kept, counted from the end.",
    )
    shell_timeout_seconds: PositiveFloat = Field(
        default=300.0,
        description="Seconds an agent's bash command may run in the sandbox before it is killed.",
    )
    command_output_limit_bytes: PositiveInt = Field(
        default=10_000,
        description="Bytes of the stdout and of the stderr of a command an agent's tool runs (bash, "
        "run_tests, run_benchmark) kept, counted from the end; small enough that both fit in one tool "
        "result (`agent_loop.tool_result_max_chars`).",
    )
    sandbox_port: int = Field(
        default=8090,
        ge=1,
        le=65535,
        description="The port of the sandbox's command server, on agent-net.",
    )
    sandbox_response_grace_seconds: PositiveFloat = Field(
        default=30.0,
        description="Seconds the episode waits for the sandbox's reply beyond the command's own timeout.",
    )
    sandbox_scratch_directories: tuple[Path, ...] = Field(
        default=(),
        description="The sandbox's HOME and temporary directories, emptied before each episode plays so an "
        "earlier episode's agents leave nothing there for the next. Empty by default, so a sandbox server "
        "started from bare settings never empties this host's; the run configs name the container's.",
    )
    checkouts_directory: Path = Field(
        default=Path("/checkouts"),
        description="Where the episode and the sandbox mount the volume of the checkouts; the sandbox runs "
        "commands only in directories under it.",
    )
    sandbox_agent_code: bool = Field(
        default=False,
        description="Run the agents' repo tests and the sealed benchmark in a throwaway container of `image` "
        "with no network, instead of in the grading process. A stack run's grading on the host always does.",
    )


class LocArenaSettings(SettingsGroup):
    """Every tunable value, one frozen group per concern."""

    inference: InferenceSettings = Field(
        default=InferenceSettings(),
        description="Sanctioned paths and quota.",
    )
    gateway: GatewaySettings = Field(
        default=GatewaySettings(),
        description="The gateway container.",
    )
    stack: StackSettings = Field(
        default=StackSettings(),
        description="The episode's compose project.",
    )
