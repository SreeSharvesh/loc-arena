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
        "openrouter_api_key), each sandbox's own token (sandbox_token), in the episode every agent's "
        "sandbox token (sandbox_token_<agent id with - as _>), in a sandbox its agent's identity on each "
        "live service with rights (identity_<service>), in a live service with rights or tools every agent's "
        "(identity_<agent id>), and in the tools gateway its generated config (agentgateway_config).",
    )

    @field_validator("upstream")
    @classmethod
    def require_https(cls, upstream: HttpUrl) -> HttpUrl:
        """Refuse an upstream the key would reach in cleartext."""
        if upstream.scheme != "https":
            raise ValueError(f"the upstream must use https, not {upstream.scheme}")
        return upstream


class StackSettings(SettingsGroup):
    """The episode's compose project (``stack:``): its images and its containers' limits."""

    image: str = Field(
        default="loc-arena:latest",
        description="The image of the gateway and the episode: the Dockerfile's `engine` target.",
    )
    sandbox_image: str = Field(
        default="loc-arena-sandbox:latest",
        description="The image agent code runs in, every agent's sandbox's and the grader's throwaway "
        "container's: the Dockerfile's `sandbox` target, which holds no harness and no scenarios.",
    )
    episode_memory_limit: str = Field(default="4g", description="Memory limit of the episode container.")
    episode_cpus: PositiveFloat = Field(default=2.0, description="CPUs the episode container may use.")
    sandbox_memory_limit: str = Field(default="2g", description="Memory limit of each agent's sandbox.")
    sandbox_cpus: PositiveFloat = Field(default=2.0, description="CPUs each agent's sandbox may use.")
    sandbox_pids_limit: PositiveInt = Field(default=1024, description="Processes an agent's sandbox may run.")
    service_memory_limit: str = Field(default="512m", description="Memory limit of each live service.")
    service_cpus: PositiveFloat = Field(default=1.0, description="CPUs each live service may use.")
    service_pids_limit: PositiveInt = Field(default=256, description="Processes each live service may run.")
    gateway_health_interval_seconds: PositiveInt = Field(
        default=2,
        description="Seconds between health probes of the gateway, of each sandbox and of each live service.",
    )
    gateway_health_retries: PositiveInt = Field(
        default=15,
        description="Failed probes of the gateway, a sandbox or a live service before giving up.",
    )
    episode_pids_limit: PositiveInt = Field(default=4096, description="Processes the episode may run.")
    tools_gateway_image: str = Field(
        default="ghcr.io/agentgateway/agentgateway:v1.5.0",
        description="The image of the tools gateway, agentgateway, pinned to a release: it puts every live "
        "service with tools on one MCP route, offers each agent its tools and passes its identity on. Its "
        "limits are a live service's.",
    )
    tools_gateway_port: int = Field(
        default=3000,
        ge=1,
        le=65535,
        description="The port of the tools gateway's MCP route (/mcp), on agent-net.",
    )
    agent_code_output_limit_bytes: PositiveInt = Field(
        default=1_000_000,
        description="Bytes of an agent-code run's stdout and of its stderr kept, counted from the end.",
    )
    shell_timeout_seconds: PositiveFloat = Field(
        default=300.0,
        description="Seconds an agent's bash command may run in its sandbox before it is killed.",
    )
    run_tests_timeout_seconds: PositiveFloat = Field(
        default=300.0,
        description="Seconds the in-process run_tests tool may run before its whole session is killed; in a "
        "stack run agents run the run-tests skill with bash, under `shell_timeout_seconds`.",
    )
    run_benchmark_timeout_seconds: PositiveFloat = Field(
        default=120.0,
        description="Seconds the in-process run_benchmark tool may run before its whole session is killed; "
        "in a stack run agents run the run-benchmark skill with bash, under `shell_timeout_seconds`.",
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
        description="The port of each sandbox's command server, on agent-net.",
    )
    sandbox_response_grace_seconds: PositiveFloat = Field(
        default=30.0,
        description="Seconds the episode waits for a sandbox's reply beyond the command's own timeout.",
    )
    sandbox_recovery_seconds: PositiveFloat = Field(
        default=60.0,
        description="Seconds an episode waits, before it plays, for each sandbox that is restarting (an "
        "earlier episode's agents killed it); then the episode fails, and runs agent code nowhere else.",
    )
    sandbox_scratch_directories: tuple[Path, ...] = Field(
        default=(),
        description="Each sandbox's HOME and temporary directories, emptied before each episode plays so an "
        "earlier episode's agents leave nothing there for the next. Empty by default, so a sandbox server "
        "started from bare settings never empties this host's; the run configs name the container's.",
    )
    tools_timeout_seconds: PositiveFloat = Field(
        default=300.0,
        description="Seconds one MCP tool call through agentgateway may take before it is an error result.",
    )
    tools_connect_seconds: PositiveFloat = Field(
        default=30.0,
        description="Seconds each agent's MCP client keeps retrying its first listing of tools, while "
        "agentgateway starts; then the episode fails.",
    )
    checkouts_directory: Path = Field(
        default=Path("/checkouts"),
        description="Where the episode and every sandbox mount the volume of the checkouts; a sandbox runs "
        "commands only in directories under it.",
    )
    skills_directory: Path = Field(
        default=Path("/skills"),
        description="Where every sandbox mounts the scenario's skills folder, read-only; outside the "
        "checkouts and the scratch directories, which a reset empties.",
    )
    sandbox_agent_code: bool = Field(
        default=False,
        description="Run the agents' repo tests and the sealed benchmark in a throwaway container of "
        "`sandbox_image` with no network, instead of in the grading process. A stack run's grading on the "
        "host always does, and its compose project builds the image; otherwise build the Dockerfile's "
        "`sandbox` target first and tag it as `sandbox_image`. Grading stops if Docker lacks it.",
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
