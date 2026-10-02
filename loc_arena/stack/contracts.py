"""Every body the stack's services exchange, and the interfaces its packages implement or consume."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PositiveInt, SecretStr, StringConstraints

from loc_arena.logging_.events import EventDraft, EventLog
from loc_arena.stack.constants import EPISODE_HANDLE_BYTES, EPISODE_HANDLE_PATTERN
from loc_arena.stack.model_call import GenerateRequest, GenerateResponse

EpisodeHandle = Annotated[str, StringConstraints(pattern=EPISODE_HANDLE_PATTERN)]
EpisodeMode = Literal["attack", "honest"]
CoverageComponent = Literal["monitor", "logging"]
CredentialKind = Literal["turn_token", "batch_id", "durable_identity", "monitor", "none"]
Phase = Literal["deciding", "executing"]
CodeToolName = Literal[
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "search_code",
    "grep",
    "run_tests",
    "run_benchmark",
    "profile",
    "bash",
]


def generate_episode_handle() -> str:
    """A fresh random episode handle, in the only form the handle routes accept."""
    return secrets.token_hex(EPISODE_HANDLE_BYTES)


class ContractModel(BaseModel):
    """Base of every body: immutable, and an unknown field is a validation error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ServiceHealth(ContractModel):
    """A service's health: up."""

    ok: bool


# --- gateway core and edge: model calls ---
class CoreHealth(ServiceHealth):
    """The core's health: up, and whether it holds a provider key."""

    provider_configured: bool


class CoreGenerateResponse(GenerateResponse):
    """The core's reply: the model call's reply, plus the episode that recorded it and the core's clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    episode_handle: EpisodeHandle
    episode_id: str
    ts: float


class RelayedGenerateResponse(CoreGenerateResponse):
    """The edge's reply: the core's, plus the mirror record's seq (``None`` when logging drops the call)."""

    mirror_seq: int | None


class BatchGenerateRequest(ContractModel):
    """A sanctioned, stateless, quota-limited batch of prompts."""

    prompts: tuple[str, ...]
    caller_identity: str
    role: str = "teacher"


class BatchGenerateResponse(ContractModel):
    """The core's reply to a batch: one completion and one sealed seq per prompt, one batch id."""

    texts: tuple[str, ...]
    batch_id: str
    seqs: tuple[int, ...]
    episode_handle: EpisodeHandle
    episode_id: str
    ts: float


class RelayedBatchGenerateResponse(BatchGenerateResponse):
    """The edge's reply to a batch: the core's, plus one mirror seq per prompt."""

    mirror_seqs: tuple[int, ...]


# --- gateway core: control routes (control key) ---
ProviderKind = Literal["openrouter", "deterministic"]


def build_episode_id(run_config_name: str, mode: EpisodeMode) -> str:
    """The id every event of an episode carries: the same rule on the core and in process (STACK=0)."""
    return f"{run_config_name}-{mode}"


class ModelRoute(ContractModel):
    """One model role as the core routes it: the model id, its default temperature, its output cap."""

    model: str
    temperature: float
    max_tokens: PositiveInt


class CovertInjection(ContractModel):
    """The covert objective the core prepends, server side, to the target identity's prompts when enabled."""

    enabled: bool
    target_identity: str
    objective_prompt: str


class EpisodeOpen(ContractModel):
    """Open an episode on the core; it becomes the active one that off-path calls are recorded under."""

    run_config_name: str
    mode: EpisodeMode
    models: dict[str, ModelRoute]
    covert: CovertInjection
    provider: ProviderKind


class EpisodeOpened(ContractModel):
    """The opened episode's handle (for every later route) and its id (in every event)."""

    handle: EpisodeHandle
    episode_id: str


class ClockUpdate(ContractModel):
    """Set the episode's simulated clock (any value, in call order)."""

    now: float


class CoverageUpdate(ContractModel):
    """Set the deployed monitor or logging coverage; ``None`` covers every identity (the baseline)."""

    component: CoverageComponent
    covered: frozenset[str] | None


class TurnTokenRequest(ContractModel):
    """Mint the token that marks an agent's model calls as inside one sanctioned turn."""

    agent_uid: str
    turn: int


class IssuedToken(ContractModel):
    """A signed turn token."""

    token: str


class DurableCredentialRequest(ContractModel):
    """Issue a durable service-account credential, or rotate one (revoke the live instance, issue anew)."""

    account: str
    sanctioned: bool
    rotate: bool


class DurableCredentialIssued(ContractModel):
    """The new credential; ``revoked_instance`` is "" unless a rotation revoked a live instance."""

    token: str
    instance: str
    revoked_instance: str


class MonitorCall(ContractModel):
    """A monitor's own model call, made by the core under a monitor credential it signs itself."""

    monitor: str
    prompt: str
    temperature: float


class MonitorCallResult(ContractModel):
    """The monitor's completion."""

    text: str


class EpisodeClosed(ContractModel):
    """The episode is closed; the last seq of its sealed log."""

    last_sealed_seq: int


# --- recorder (sealed-net) and edge mirror ---
class AppendAck(ContractModel):
    """The recorder wrote the event; its fingerprint."""

    fp: str


class ModelCallRecord(ContractModel):
    """One provider call as the core made it (covert injection included), for the sealed model-call log."""

    sealed_seq: int
    identity: str
    role: str
    model_input: str
    output: str
    wall_ts: float


class MirrorAppend(ContractModel):
    """An observable event for the edge to write to the episode's mirror log (control key)."""

    episode_id: str
    draft: EventDraft


# --- execution app (one per agent sandbox) ---
class WorkspaceCreate(ContractModel):
    """Prepare the sandbox's workspace for an episode."""

    handle: EpisodeHandle


class CodeToolCall(ContractModel):
    """One allowlisted code tool call and its arguments as the model wrote them."""

    tool: CodeToolName
    arguments: dict[str, JsonValue]


class CodeToolResult(ContractModel):
    """The tool's result, as the agent sees it."""

    result: dict[str, JsonValue]


class BashResult(ContractModel):
    """The ``bash`` tool's result: ``exit_code`` is ``None`` when the command timed out."""

    exit_code: int | None
    output: str
    truncated: bool
    timed_out: bool


class ExecutionHealth(ServiceHealth):
    """The execution app's health: up, and the agent whose sandbox it serves."""

    agent_id: str


# --- grader (its stdout is untrusted) and the sealed reference ---
class GradeMeasurement(ContractModel):
    """What the Aurora scorer's measure step reports on one checkout."""

    suite_green: bool
    pipeline_completed: bool
    cost: int | None
    outputs: dict[str, int | float]


class ExpertBaseline(ContractModel):
    """The expert's score on the main task and the hours it took."""

    score: float
    hours: float


class GradeReference(ContractModel):
    """A scenario's sealed ``reference.json``, parsed on the host only."""

    note: str = Field(default="", alias="_note")
    baseline_cost: int
    reference_cost: int
    reference_score: float
    reference_outputs: dict[str, int | float]
    tolerance: float
    min_work: int
    expert_baseline: ExpertBaseline


# --- the runner's per-episode export (the host reads it back) ---
class TurnReference(ContractModel):
    """One agent turn (``agent_trace.TurnRef``)."""

    agent_uid: str
    turn: int


class TurnTiming(ContractModel):
    """The wall-clock bounds of one completed turn (``agent_trace.TurnRecord``)."""

    agent_uid: str
    turn: int
    wall_start: float
    wall_end: float


class MonitorVerdictRecord(ContractModel):
    """A monitor's score for one mirror event (``monitors.registry.MonitorVerdict``)."""

    monitor: str
    tap: str
    score: float
    reason: str
    actor_uid: str
    target_seq: int | None
    ts: float


class EpisodeLanes(ContractModel):
    """The turn each sealed and mirror seq happened in (``None`` outside every turn)."""

    sealed: dict[int, TurnReference | None]
    mirror: dict[int, TurnReference | None]


class RunnerEpisodeExport(ContractModel):
    """What the runner knows of one episode that the sealed logs do not: verdicts, turns and their lanes."""

    handle: EpisodeHandle
    episode_id: str
    deadline_ts: float
    last_sealed_seq: int
    verdicts: tuple[MonitorVerdictRecord, ...]
    turns: tuple[TurnTiming, ...]
    lanes: EpisodeLanes
    phases: dict[int, Phase | None]
    mirror_to_sealed: dict[int, int]


# --- in-process interfaces ---
@dataclass(frozen=True)
class DurableCredential:
    """A durable service-account credential: the signed token (masked when printed) and its instance."""

    token: SecretStr
    instance: str


@dataclass(frozen=True)
class CredentialRotation:
    """A real rotation: the instance it revoked ("" when the account held none) and the new credential."""

    revoked_instance: str
    credential: DurableCredential


class GatewayControl(Protocol):
    """The runner's control of one episode on the core, in process or over the control routes."""

    def set_clock(self, now: float) -> None:
        """Set the simulated clock the core stamps records and verifies tokens with."""
        ...

    def set_coverage(self, component: CoverageComponent, covered: frozenset[str] | None) -> None:
        """Set the deployed monitor or logging coverage (``None`` covers everyone)."""
        ...

    def mint_turn_token(self, agent_uid: str, turn: int) -> str:
        """A token marking ``agent_uid``'s calls in ``turn`` as sanctioned."""
        ...

    def issue_durable_credential(self, account: str, *, sanctioned: bool) -> DurableCredential:
        """Issue a durable credential for ``account``, recording truthfully whether it was sanctioned."""
        ...

    def rotate_durable_credential(self, account: str, *, sanctioned: bool) -> CredentialRotation:
        """Revoke ``account``'s live instance and issue a new one."""
        ...

    def call_model_as_monitor(self, monitor: str, prompt: str, temperature: float) -> str:
        """Make a monitor's own model call through the core (a sealed, attributable record)."""
        ...

    def close(self) -> int:
        """Close the episode and return the last seq of its sealed log."""
        ...


class Servable(Protocol):
    """The gateway as a model client reaches it: the edge in process, or an HTTP client of the edge."""

    def generate(self, request: GenerateRequest, /) -> RelayedGenerateResponse:
        """Relay one model call and mirror it."""
        ...

    def batch_generate(self, request: BatchGenerateRequest, /) -> RelayedBatchGenerateResponse:
        """Relay one batch and mirror each prompt."""
        ...


class CoreRelay(Protocol):
    """The core's model routes as the edge relays to them: the core in process, or its HTTP client."""

    def generate(self, request: GenerateRequest, /) -> CoreGenerateResponse:
        """Serve one model call and record it sealed."""
        ...

    def batch_generate(self, request: BatchGenerateRequest, /) -> BatchGenerateResponse:
        """Serve one batch and record each prompt sealed."""
        ...


class CodeToolRunner(Protocol):
    """Runs one agent's code tools: a local workspace, or the execution app in that agent's sandbox."""

    def run(self, call: CodeToolCall, /) -> CodeToolResult:
        """Run one allowlisted tool call."""
        ...


@dataclass(frozen=True)
class EpisodeWiring:
    """Everything the scaffold talks to for one episode, the same shape in process and in the stack."""

    handle: EpisodeHandle
    gateway: Servable
    control: GatewayControl
    sealed: EventLog
    mirror: EventLog
    code_tools: Mapping[str, CodeToolRunner]
