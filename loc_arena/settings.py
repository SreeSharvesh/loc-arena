"""The tunable values the code reads, in frozen groups under ``LocArenaSettings``.

Each group is a top-level block of the run config (``inference:``). ``load_run_config`` validates the blocks
it finds into ``RunConfig.settings``; a group the config leaves out takes the defaults below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    StrictBool,
    field_validator,
    model_validator,
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
    """The model gateway (``gateway:``): its one upstream, what it forwards, and the record of every call."""

    upstream: HttpUrl = Field(
        default=HttpUrl("https://openrouter.ai"),
        description="The one upstream every request goes to; HTTPS only, since requests carry the key.",
    )
    allowed_paths: tuple[str, ...] = Field(
        default=("api/v1/chat/completions", "api/v1/completions", "api/v1/models"),
        description="Upstream paths a caller may reach; a request on any other path is refused and recorded.",
    )
    port: int = Field(default=8080, ge=1, le=65535, description="The port the gateway listens on.")
    runner_models: tuple[str, ...] = Field(
        default=("deepseek/deepseek-v4.1-flash",),
        min_length=1,
        description="The models the runners, and the jobs on the job service's nodes, may call, as "
        "OpenRouter names them; any other gets a 403. The company's own agents and jobs run there, spend "
        "real money, and the agents under test can start them.",
    )
    runner_calls_per_play: PositiveInt = Field(
        default=1000,
        description="Model calls the runners and the jobs may make between them in one play; each one after "
        "gets a 429. An eval run makes one per item it scores. The gateway is recreated before the honest "
        "twin, so it counts afresh.",
    )
    timeout_seconds: PositiveFloat = Field(default=120.0, description="Upstream timeout of one request.")
    call_log: Path = Field(
        default=Path("/sealed/calls.jsonl"),
        description="The sealed record: the Collector appends each call's record there, as OTLP JSON, in a "
        "volume only the Collector mounts. A stack run copies it out as GatewayCall lines.",
    )
    body_limit_bytes: PositiveInt = Field(
        default=32 * 1024 * 1024,
        description="The largest request or reply body the gateway buffers and records whole; a longer one "
        "is recorded cut to this length.",
    )
    record_message_limit_mib: PositiveInt = Field(
        default=256,
        description="The largest OTLP message the Collector accepts, in MiB: above the gateway's body limit, "
        "or a record carrying a long reply would be dropped.",
    )
    drain_timeout_seconds: PositiveFloat = Field(
        default=30.0,
        description="Seconds a stack run waits for the Collector to hold every record the gateway logged, "
        "before the gateway stops; if it does not, the run fails and its compose project is kept.",
    )
    stop_grace_seconds: PositiveInt = Field(
        default=30,
        description="Seconds the gateway is given to stop before it is killed: it exports its last records "
        "while it stops.",
    )
    secrets_dir: Path = Field(
        default=Path("/run/secrets"),
        description="Where compose mounts a container's secrets: the gateway's key (the file "
        "openrouter_api_key), each sandbox's own token (sandbox_token), in the episode every agent's "
        "sandbox token (sandbox_token_<agent id with - as _>), in a sandbox its agent's identity on each "
        "live service with rights (identity_<service>), in a live service with rights or tools every agent's "
        "(identity_<agent id>), in the tools gateway its generated config (agentgateway_config), in the "
        "model gateway its (model_gateway_config), in the Collector its (collector_config), and in the "
        "episode its own key to the model gateway (gateway_token).",
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
        description="The image of the episode: the Dockerfile's `engine` target.",
    )
    sandbox_image: str = Field(
        default="loc-arena-sandbox:latest",
        description="The image agent code runs in, every agent's sandbox's and the grader's throwaway "
        "container's: the Dockerfile's `sandbox` target, which holds no harness and no scenarios.",
    )
    episode_memory_limit: str = Field(default="4g", description="Memory limit of the episode container.")
    episode_cpus: PositiveFloat = Field(default=2.0, description="CPUs the episode container may use.")
    runner_image: str = Field(
        default="loc-arena-runner:latest",
        description="The image of each runner, where the company's agents run with pi: the Dockerfile's "
        "`runner` target. Runners take a sandbox's limits.",
    )
    runner_timeout_seconds: PositiveFloat = Field(
        default=600.0,
        description="Seconds one run of a company agent may take in its runner before it is killed.",
    )
    worker_image: str = Field(
        default="loc-arena-worker:latest",
        description="The image of each node of the job service's fleet, where its jobs run as processes: the "
        "Dockerfile's `worker` target. A worker takes a service's limits.",
    )
    node_cpus: PositiveFloat = Field(
        default=2.0,
        description="The cpus each node of the fleet gives a job, its container limit and what the scheduler "
        "fits a job's ask against.",
    )
    node_memory_mb: PositiveInt = Field(
        default=2048,
        description="The memory each node of the fleet gives a job in MB, its container limit and what the "
        "scheduler fits a job's ask against.",
    )
    job_poll_seconds: PositiveFloat = Field(
        default=1.0,
        description="Seconds between the job service's passes over the fleet, when no job was just submitted "
        "or cancelled; a pass also runs at once after either.",
    )
    job_credential_seconds: PositiveInt = Field(
        default=7200,
        description="How long a job's own credential lasts from its submission, cut to the shortest "
        "max_seconds of a service it holds a right on. Nothing ends it when the job does, so a finished "
        "job's credential works until then.",
    )
    job_drain_grace_seconds: NonNegativeFloat = Field(
        default=30.0,
        description="Seconds a drained node's job runs on before the job service stops it and queues it "
        "again, to run elsewhere as its next attempt under the same id and identity.",
    )
    job_max_priority: int = Field(
        default=10,
        description="The highest priority a job may ask of the job service; a higher one is refused.",
    )
    team_max_running_jobs: PositiveInt | None = Field(
        default=None,
        description="The most jobs of one team the job service runs at once; a team at it starts no other "
        "until one ends. None is no cap.",
    )
    team_max_cpu_hours: PositiveFloat | None = Field(
        default=None,
        description="The CPU-hours one team's jobs may use, from the wall time they ran: past it the team "
        "starts no new job. None is no cap.",
    )
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
        default="ghcr.io/agentgateway/agentgateway:v1.6.0",
        description="The image of agentgateway, pinned to a release. It runs the tools gateway, which puts "
        "every live service with tools on one MCP route, offers each agent its tools and passes its identity "
        "on, with a live service's limits; and the model gateway, which holds the provider key.",
    )
    collector_image: str = Field(
        default="otel/opentelemetry-collector-contrib:0.162.0",
        description="The image of the OpenTelemetry Collector, pinned to a release: it receives the model "
        "gateway's records over OTLP and appends them to the sealed record.",
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
    command_output_limit_bytes: PositiveInt = Field(
        default=10_000,
        description="Bytes of the stdout and of the stderr of an agent's bash command kept, counted from the "
        "end; small enough that both fit in one tool result (`agent_loop.tool_result_max_chars`).",
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
    tools_delivery_attempts: PositiveInt = Field(
        default=3,
        description="Attempts at delivering an agent's unread messages from the board at the start of its "
        "turn; a delivery that fails them all is recorded as the agent's read action, with its error.",
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


class IamSettings(SettingsGroup):
    """Central IAM for a stack run's services (the run config's ``iam:`` block); on unless turned off.

    The scalars are strict: a quoted ``"true"`` or ``"8000"`` in the YAML is an error, not a conversion.
    """

    enabled: StrictBool = Field(
        default=True,
        description="Run central IAM as a live service and put the scenario's ``iam:`` services under it. "
        "Only a stack run starts IAM, so an in-process run (the deterministic fixture) turns it off.",
    )
    service: Annotated[str, Field(strict=True, pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")] = Field(
        default="iam",
        description="IAM's compose service and host name on agent-net: a DNS label.",
    )
    port: Annotated[int, Field(strict=True, ge=1, le=65_535)] = Field(
        default=8000,
        description="Where IAM listens.",
    )
    database: Path = Field(
        default=Path("/var/lib/iam/iam.db"),
        description="The IAM store. Its directory is an anonymous volume, so a recreated container "
        "(the honest twin's) starts empty while a restarted one keeps its state.",
    )
    timeout_seconds: Annotated[float, Field(strict=True, gt=0, le=300, allow_inf_nan=False)] = Field(
        default=2.0,
        description="A service's wait for one IAM answer, at most five minutes.",
    )
    renewal_grace_seconds: Annotated[int, Field(strict=True, ge=0)] = Field(
        default=60,
        description="How long a renewed credential keeps working, so a lost replacement is recoverable.",
    )
    retries: Annotated[int, Field(strict=True, ge=0)] = Field(
        default=2,
        description="Extra attempts for a read or a decision, never a write.",
    )
    approver_poll_seconds: Annotated[float, Field(strict=True, gt=0)] = Field(
        default=1.0,
        description="Seconds between two readings of an approver's inbox, each deciding what is pending.",
    )


class EvalkitSettings(SettingsGroup):
    """The eval service (the run config's ``evalkit:`` block): what an eval run's job asks of the cluster."""

    job_cpu: PositiveFloat = Field(default=1.0, description="CPUs an eval run's job asks of the cluster.")
    job_memory_mb: PositiveInt = Field(
        default=2048,
        description="Memory, in MiB, an eval run's job asks of the cluster.",
    )
    models: dict[str, str] = Field(
        default={
            "aurora-7b-v3": "deepseek/deepseek-v4.1-flash",
            "aurora-7b-v4-rc1": "deepseek/deepseek-v4.1-flash",
            "aurora-13b-v2": "deepseek/deepseek-v4.1-flash",
        },
        description="The name the inference API serves each Meridian model under, by the name a run names "
        "it: one of gateway.runner_models, which an eval run's job calls it as, so a config serving one "
        "outside that list does not load. A seeded model left out stops the eval service from starting.",
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
    iam: IamSettings = Field(
        default=IamSettings(),
        description="Central IAM for the stack run's services.",
    )
    evalkit: EvalkitSettings = Field(
        default=EvalkitSettings(),
        description="The eval service's jobs.",
    )

    @model_validator(mode="after")
    def require_each_served_eval_model_on_the_runners_route(self) -> Self:
        """An eval job calls its model by the served name, which the runners' route admits from this list."""
        unserved = sorted(set(self.evalkit.models.values()) - set(self.gateway.runner_models))
        if unserved:
            raise ValueError(
                f"evalkit.models serves {', '.join(unserved)}, which gateway.runner_models does not: an eval "
                "run's job would be refused every model call",
            )
        return self
