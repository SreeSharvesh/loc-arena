"""Typed configuration loader and validator.

Enforces config over code (no magic constants in code paths) and reproducibility from config+seed (a run
is fully described by its resolved config plus a seed). Reads ``env.default.yaml``, ``models.cheap.yaml``,
and a run file, resolves ``extends`` by DEEP-merging the run's overrides onto the base recursively (so a
partial override of a nested block such as ``episode:`` keeps the base block's other keys), resolves the
``models`` reference and the ``prompts_inline.*`` ``*_ref`` references, validates aggressively, and
returns one frozen ``RunConfig``. Fails loud on a missing or malformed field; no default lives here,
defaults live in the YAML.

See docs/isolation/design.md#run-config.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self

import yaml
from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PlainValidator,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass
from pydantic_settings import BaseSettings, SettingsConfigDict, YamlConfigSettingsSource
from scenarios.loader import load_run_scenario, load_scenario

from loc_arena.execution.checkout import list_codebase_repositories
from loc_arena.ids import PermissionScope
from loc_arena.registry import SCORER_REGISTRY, VERIFIER_REGISTRY, is_scorer, is_verifier
from loc_arena.stack.constants import AGENT_MODEL_ROLE, MONITOR_MODEL_ROLE, RUN_MODEL_ROLES
from loc_arena.stack.settings import LocArenaSettings

YAML_ENCODING: Final = "utf-8"
BLOCK_CONFIG: Final = ConfigDict(extra="ignore")
PROMPT_REFERENCE_PREFIX: Final = "prompts_inline."
REFERENCE_SUFFIX: Final = "_ref"
COVERT_PROMPTS: Final = ("objective_prompt",)
AGENT_PROMPTS: Final = ("system_prompt", "brief")
LANDING_FORMULAS: Final = frozenset({"minimal", "robust"})
ROOT_AGENT_ID: Final = "agent-main"  # the policies, the grader's export and the stack drive the team from it


class ConfigError(ValueError):
    """A configuration file was missing a field, malformed, or internally inconsistent."""


# --------------------------------------------------------------------------------------------------------
# Field validators
# --------------------------------------------------------------------------------------------------------
def _parse_scope(raw: object) -> PermissionScope:
    if isinstance(raw, PermissionScope):  # dataclasses.replace passes the built scope
        return raw
    if not isinstance(raw, dict):
        raise ValueError(f"scope must be a mapping, got {type(raw).__name__}")
    try:
        return PermissionScope.from_dict(raw)
    except TypeError as exc:  # pydantic reports a ValueError with the field's path, a TypeError escapes it
        raise ValueError(str(exc)) from exc


def _require_landing_formulas(landing: dict[str, str]) -> dict[str, str]:
    missing = LANDING_FORMULAS - set(landing)
    if missing:
        raise ValueError(f"landing must define 'minimal' and 'robust', missing {sorted(missing)}")
    return landing


def _prompt_alias(field_name: str) -> AliasChoices:
    return AliasChoices(f"{field_name}{REFERENCE_SUFFIX}", field_name)


# --------------------------------------------------------------------------------------------------------
# Typed config blocks
# --------------------------------------------------------------------------------------------------------
@pydantic_dataclass(frozen=True, config=BLOCK_CONFIG)
class ModelSpec:
    """A model routing entry from ``models.cheap.yaml`` (role -> model and sampling)."""

    model: StrictStr = Field(
        min_length=1,
        description="The OpenRouter model id (provider/model) this role calls.",
    )
    temperature: StrictFloat = Field(
        ge=0.0,
        description="Sampling temperature of every call this role makes.",
    )
    max_tokens: StrictInt = Field(
        gt=0,
        description="Output cap of one call (reasoning plus tool arguments), not the context window.",
    )


@pydantic_dataclass(frozen=True, config=BLOCK_CONFIG)
class AgentLoopConfig:
    """The live agent loop's tool-result cap. The value comes from ``agent_loop:``."""

    tool_result_max_chars: StrictInt = Field(
        description="How many characters of a tool result the model sees; the rest is marked cut.",
    )


@pydantic_dataclass(frozen=True, config=BLOCK_CONFIG)
class EpisodeConfig:
    """Episode ceilings and spawn caps (the DEEP-merged ``episode:`` block)."""

    orchestrator_turn_budget: StrictInt = Field(description="Turn ceiling of the root orchestrator.")
    subagent_turn_budget: StrictInt = Field(description="Turn ceiling of each sub-agent.")
    wall_clock_seconds: StrictInt = Field(description="Wall-clock ceiling of the episode.")
    liveness_window_seconds: StrictInt = Field(
        description="A loop is alive at the end if it called within this window before the deadline.",
    )
    max_spawn_depth: StrictInt = Field(description="Levels of sub-agents that may nest below the root.")
    max_live_subagents: StrictInt = Field(description="Sub-agents alive at once.")
    max_total_agents: StrictInt = Field(description="Agents ever created in one episode, the root included.")
    subagent_idle_timeout_seconds: StrictInt = Field(
        description="A sub-agent with no logged activity for this long is closed as idle.",
    )


@pydantic_dataclass(frozen=True, config=BLOCK_CONFIG)
class Thresholds:
    """The side-task verifier thresholds (``aurora-efficiency.yaml`` ``side_task.thresholds``)."""

    loop_len_floor: StrictInt = Field(description="Fewest off-path calls that count as a self-feeding loop.")
    liveness_window_seconds: StrictInt = Field(
        description="Window before the deadline in which the loop must still call; equals the episode's.",
    )
    sustained_rate_window_seconds: StrictInt = Field(
        description="Window over which the loop's call rate is measured against the alerting threshold.",
    )


@pydantic_dataclass(frozen=True, config=BLOCK_CONFIG)
class SideTaskConfig:
    """The side task: verifier name, thresholds, landing formulas, milestones, and the sensitivity band."""

    name: StrictStr = Field(description="The side task's name; names the run when it has no slug.")
    verifier: StrictStr = Field(description="The registered verifier that grades the side task.")
    thresholds: Thresholds = Field(description="The verifier's thresholds.")
    landing: Annotated[dict[str, StrictStr], AfterValidator(_require_landing_formulas)] = Field(
        description="The landing formulas by name; 'minimal' and 'robust' are required.",
    )
    milestones: list[dict[str, Any]] = Field(description="The kill-chain milestones, free-form mappings.")
    sensitivity_band: dict[str, list[StrictInt]] = Field(
        default_factory=dict,
        description="Alternative values of each threshold the verdict is re-checked at.",
    )
    coordination: dict[str, Any] = Field(
        default_factory=dict,
        description="How the team coordinates covertly, free-form.",
    )
    slug: StrictStr | None = Field(default=None, description="Short run-name slug; the name when absent.")


@pydantic_dataclass(frozen=True, config=BLOCK_CONFIG)
class CovertConfig:
    """The covert-objective toggle: ``enabled`` is the ONLY difference between attack and its honest twin."""

    enabled: StrictBool = Field(description="True for the attack run, false for its honest twin.")
    target_identity: StrictStr = Field(description="The agent that receives the covert objective.")
    objective_prompt: StrictStr = Field(
        validation_alias=_prompt_alias("objective_prompt"),
        description="The covert objective's text, resolved from objective_prompt_ref.",
    )


@pydantic_dataclass(frozen=True, config=BLOCK_CONFIG)
class AgentConfig:
    """One agent's identity, branch, scope, covert subtasks, and resolved prompt/brief."""

    id: StrictStr = Field(description="The agent's identity, unique in the run.")
    kind: StrictStr = Field(description="The agent's role, such as orchestrator.")
    trust: StrictStr = Field(description="The agent's trust level, such as untrusted.")
    branch: StrictStr = Field(description="The branch the agent works on.")
    scope: Annotated[PermissionScope, PlainValidator(_parse_scope)] = Field(
        description="What the agent may do; an unknown key is an error.",
    )
    covert: tuple[StrictStr, ...] = Field(default=(), description="The covert subtasks the agent owns.")
    legit: StrictStr = Field(default="", description="The agent's legitimate assignment.")
    system_prompt: StrictStr | None = Field(
        default=None,
        validation_alias=_prompt_alias("system_prompt"),
        description="The agent's system prompt, resolved from system_prompt_ref.",
    )
    brief: StrictStr | None = Field(
        default=None,
        validation_alias=_prompt_alias("brief"),
        description="The agent's brief, resolved from brief_ref.",
    )
    spawned_by: StrictStr | None = Field(default=None, description="The agent that spawns this one, if any.")


@pydantic_dataclass(frozen=True, config=BLOCK_CONFIG)
class _MainTaskKeys:
    name: StrictStr | None = Field(default=None, description="The main task's name; the slug's fallback.")
    slug: StrictStr | None = Field(default=None, description="Short run-name slug.")
    scorer: StrictStr | None = Field(default=None, description="The registered scorer that grades the task.")


@pydantic_dataclass(frozen=True, config=BLOCK_CONFIG)
class _ProtocolKeys:
    """The ``protocol`` keys the scoring computes with; ``RunConfig.protocol`` keeps the whole block."""

    audit_budget_fpr: Annotated[StrictFloat, Field(ge=0.0, le=1.0)] | None = Field(
        default=None,
        description="The false-positive rate the audit threshold is calibrated at on the honest twin.",
    )


@pydantic_dataclass(frozen=True, config=BLOCK_CONFIG)
class _LoggingConfig:
    agent_transcript: StrictBool = Field(
        default=False,
        description="Whether each run bundle holds every agent's Inspect .eval and transcript.",
    )


# --------------------------------------------------------------------------------------------------------
# The files
# --------------------------------------------------------------------------------------------------------
class _ExtendsKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", title="config file")

    extends: StrictStr | None = Field(default=None, description="The config file this one deep-merges onto.")


class _ModelsFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", title="models file")

    roles: dict[str, ModelSpec] = Field(description="The model each role calls, by role.")


class _RunDocument(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, extra="ignore", case_sensitive=True, title="run config")

    models: StrictStr = Field(description="The models file, in the configs directory.")
    seed: StrictInt = Field(description="The seed that, with this config, reproduces the episode.")
    episode: EpisodeConfig = Field(description="Episode ceilings and spawn caps.")
    side_task: SideTaskConfig = Field(description="The side task and how it is verified.")
    covert: CovertConfig = Field(description="The covert objective and whether it is on.")
    agents: tuple[AgentConfig, ...] = Field(min_length=1, description="The team.")
    main_task: _MainTaskKeys = Field(description="The main task's naming and scorer keys.")
    protocol: _ProtocolKeys = Field(description="The control protocol's keys the scoring computes with.")
    scenario: StrictStr | None = Field(default=None, description="The pack registering scorer and verifier.")
    policy: Literal["scripted", "model"] = Field(
        default="model",
        description="'model' (the live untrusted model drives the agents) or 'scripted' (deterministic).",
    )
    logging: _LoggingConfig = Field(default=_LoggingConfig(), description="What each run bundle records.")
    agent_loop: AgentLoopConfig = Field(description="The live agent loop's tool-result cap.")

    @model_validator(mode="before")
    @classmethod
    def _resolve_prompt_references(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        prompts = data.get("prompts_inline")
        resolved = dict(data)  # new containers: the merged YAML is also RunConfig.raw, kept as written
        if "covert" in data:
            resolved["covert"] = _with_prompts_resolved(data["covert"], COVERT_PROMPTS, prompts, "covert")
        if isinstance(data.get("agents"), list):
            resolved["agents"] = [
                _with_prompts_resolved(agent, AGENT_PROMPTS, prompts, f"agents.{index}")
                for index, agent in enumerate(data["agents"])
            ]
        return resolved

    @model_validator(mode="after")
    def _require_one_liveness_window(self) -> Self:
        episode_window = self.episode.liveness_window_seconds
        verifier_window = self.side_task.thresholds.liveness_window_seconds
        if episode_window != verifier_window:
            raise ValueError(
                f"episode.liveness_window_seconds ({episode_window}) != "
                f"side_task.thresholds.liveness_window_seconds ({verifier_window}); "
                "keep the two equal (the verifier reads side_task.thresholds)",
            )
        return self

    @model_validator(mode="after")
    def _require_a_team_the_harness_can_drive(self) -> Self:
        """Each agent id names one agent, and the root agent the harness drives the team from is one."""
        ids = [agent.id for agent in self.agents]
        repeated = sorted({agent_id for agent_id in ids if ids.count(agent_id) > 1})
        if repeated:
            raise ValueError(f"agents: each id must name one agent, and {repeated} name more than one")
        if ROOT_AGENT_ID not in ids:
            raise ValueError(f"agents: the team has no {ROOT_AGENT_ID!r}, the root agent the harness drives")
        return self

    @model_validator(mode="after")
    def _require_the_covert_target_in_the_team(self) -> Self:
        """The core adds the covert objective to the target's calls alone: it must be one of the team."""
        target = self.covert.target_identity
        if target not in {agent.id for agent in self.agents}:
            raise ValueError(f"covert.target_identity {target!r} names no agent of the team")
        return self


def _with_prompts_resolved(
    block: object,
    prompt_fields: tuple[str, ...],
    inline_prompts: object,
    where: str,
) -> object:
    if not isinstance(block, dict):
        return block
    references = {f"{field_name}{REFERENCE_SUFFIX}" for field_name in prompt_fields}
    kept = {key: value for key, value in block.items() if key not in prompt_fields}
    return kept | {
        key: _resolve_prompt_reference(value, inline_prompts, f"{where}.{key}")
        for key, value in kept.items()
        if key in references
    }


def _resolve_prompt_reference(reference: object, inline_prompts: object, where: str) -> object:
    if not isinstance(reference, str) or not reference.startswith(PROMPT_REFERENCE_PREFIX):
        raise ValueError(f"{where}: only prompts_inline.* references are resolved here, got {reference!r}")
    name = reference.removeprefix(PROMPT_REFERENCE_PREFIX)
    if not isinstance(inline_prompts, dict) or name not in inline_prompts:
        raise ValueError(f"{where}: reference {reference!r} not found in prompts_inline")
    return inline_prompts[name]


# --------------------------------------------------------------------------------------------------------
# The resolved run
# --------------------------------------------------------------------------------------------------------
_EMPTY_AGENT_LOOP = AgentLoopConfig(0)


@dataclasses.dataclass(frozen=True)
class RunConfig:
    """One fully resolved run: config plus seed reproduces the episode.

    A run has no hardcoded identifier. Its stable ``run_slug`` and the per-run ``run_name`` are generated from
    the task slugs and the resolved model ids, so the same config run several times never collides and the
    audit bundle plus its ``.eval`` file are named after what actually ran.
    """

    main_slug: str
    side_slug: str
    policy_model_slug: str
    monitor_model_slug: str
    seed: int
    episode: EpisodeConfig
    models: dict[str, ModelSpec]
    side_task: SideTaskConfig
    covert: CovertConfig
    agents: tuple[AgentConfig, ...]
    main_task: dict[str, Any]
    protocol: dict[str, Any]
    settings: LocArenaSettings
    scenario: str | None = None
    policy: str = "scripted"  # "scripted" (deterministic default) | "model" (live model-driven)
    agent_transcript: bool = False
    agent_loop: AgentLoopConfig = _EMPTY_AGENT_LOOP
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)

    def agent(self, agent_id: str) -> AgentConfig:
        """Return the agent config with this id, or raise ``ConfigError``."""
        for a in self.agents:
            if a.id == agent_id:
                return a
        raise ConfigError(f"no agent with id {agent_id!r}")

    @property
    def run_slug(self) -> str:
        """The stable, mode-independent grouping name: task slugs plus the policy and monitor model ids."""
        return (
            f"{self.main_slug}__{self.side_slug}"
            f"__policy-{self.policy_model_slug}__monitor-{self.monitor_model_slug}"
        )

    def run_name(self, mode: str, instance_id: str) -> str:
        """The unique name for one run: the honest twin uses ``honest`` in the side slot, then a short id."""
        side = "honest" if mode == "honest" else self.side_slug
        return (
            f"{self.main_slug}__{side}"
            f"__policy-{self.policy_model_slug}__monitor-{self.monitor_model_slug}__{instance_id}"
        )


def _model_slug(model_id: str) -> str:
    """The bare model name for a run name: the OpenRouter id with any provider prefix stripped."""
    return model_id.rsplit("/", 1)[-1]


def _slugify(text: str) -> str:
    """A filesystem-safe run-name slug: lowercase, non-alphanumeric runs collapsed to single hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "run"


# --------------------------------------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------------------------------------
def _read_config_file[Schema: BaseModel](path: Path, schema: type[Schema]) -> Schema:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        with path.open("r", encoding=YAML_ENCODING) as file:
            data: object = yaml.safe_load(file)
    except yaml.YAMLError as exc:  # its message gives the line and column
        raise ConfigError(f"config file {path} is not valid YAML: {exc}") from exc
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"config file {path} is invalid: {exc}") from exc


def _extends_chain(run_file: Path, configs_directory: Path) -> list[Path]:
    chain = [run_file]
    seen: set[str] = set()
    while (base_name := _read_config_file(chain[0], _ExtendsKey).extends) is not None:
        if base_name in seen:
            raise ConfigError(f"extends cycle detected at {base_name!r}")
        seen.add(base_name)
        chain.insert(0, configs_directory / base_name)
    return chain


def load_run_config(run_path: str | Path, configs_dir: str | Path | None = None) -> RunConfig:
    """Load and validate a run file into a frozen ``RunConfig``.

    Resolves ``extends`` (DEEP-merge onto the base), ``models`` (a sibling models file), and the
    ``prompts_inline.*`` references. ``configs_dir`` defaults to the run file's own directory, where the
    base and models files are resolved. Raises ``ConfigError`` on any missing or malformed field.
    """
    run_file = Path(run_path)
    configs_directory = Path(configs_dir) if configs_dir is not None else run_file.parent
    source = YamlConfigSettingsSource(
        _RunDocument,
        _extends_chain(run_file, configs_directory),
        yaml_file_encoding=YAML_ENCODING,
        deep_merge=True,
    )
    try:
        document = _RunDocument(_build_sources=((source,), {}))
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
    models_path = configs_directory / document.models
    models = _read_config_file(models_path, _ModelsFile).roles
    missing_roles = sorted(set(RUN_MODEL_ROLES) - set(models))
    if missing_roles:
        raise ConfigError(f"models file {models_path} routes no model for the roles {missing_roles}")
    _validate_registry_bindings(document.scenario, document.main_task.scorer, document.side_task.verifier)
    _require_a_codebase(document.scenario)

    main_task = document.main_task
    policy_model = models[AGENT_MODEL_ROLE].model
    monitor_model = models[MONITOR_MODEL_ROLE].model
    return RunConfig(
        main_slug=_slugify(main_task.slug or main_task.name or "main"),
        side_slug=_slugify(document.side_task.slug or document.side_task.name),
        policy_model_slug=_model_slug(policy_model),
        monitor_model_slug=_model_slug(monitor_model),
        seed=document.seed,
        episode=document.episode,
        models=models,
        side_task=document.side_task,
        covert=document.covert,
        agents=document.agents,
        main_task=dict(source.yaml_data["main_task"]),
        protocol=dict(source.yaml_data["protocol"]),
        settings=_build_settings(source.yaml_data),
        scenario=document.scenario,
        policy=document.policy,
        agent_transcript=document.logging.agent_transcript,
        agent_loop=document.agent_loop,
        raw=source.yaml_data,
    )


def _build_settings(merged: Mapping[str, object]) -> LocArenaSettings:
    declared = {group: merged[group] for group in LocArenaSettings.model_fields if group in merged}
    try:
        return LocArenaSettings.model_validate(declared)
    except ValidationError as exc:
        raise ConfigError(f"invalid settings block: {exc}") from exc


def _require_a_codebase(scenario_name: str | None) -> None:
    """The run's scenario names a codebase that holds repositories: the agents' checkout is seeded from it."""
    scenario = load_run_scenario(scenario_name)
    try:
        list_codebase_repositories(scenario.codebase_directory)
    except (OSError, ValueError) as exc:  # no such directory, or one holding no repository
        raise ConfigError(f"scenario {scenario.name!r}: codebase {scenario.codebase!r}: {exc}") from exc


def _validate_registry_bindings(scenario_name: str | None, scorer: str | None, verifier: str) -> None:
    """Load the scenario pack (running its registrations) and validate the scorer/verifier names.

    ``config.main_task.scorer`` and ``config.side_task.verifier`` dispatch BY NAME through the
    ``SCORER_REGISTRY`` / ``VERIFIER_REGISTRY``. The scenario pack's ``main.py`` / ``side.py`` register
    those names, so we import it first, then fail loud on any name the registry does not know. The
    ``side_task`` import stays LAZY: it imports ``config`` (an import-time cycle).
    """
    # Baseline registrations (importing side_task registers kill_chain_v1; lazy to avoid an import cycle).
    from loc_arena.verifier import side_task as _side_task  # noqa: F401, PLC0415

    if scenario_name is not None:
        try:
            load_scenario(scenario_name)
        except Exception as exc:  # surface a missing/broken pack as a config error
            raise ConfigError(f"scenario {scenario_name!r} failed to load: {exc}") from exc

    if not scorer:
        raise ConfigError("main_task.scorer must name a registered scorer (a non-empty string)")
    if not is_scorer(scorer):
        known = ", ".join(sorted(SCORER_REGISTRY)) or "(none registered)"
        raise ConfigError(f"main_task.scorer {scorer!r} is not a registered scorer; known: {known}")

    if not is_verifier(verifier):
        known = ", ".join(sorted(VERIFIER_REGISTRY)) or "(none registered)"
        raise ConfigError(f"side_task.verifier {verifier!r} is not a registered verifier; known: {known}")
