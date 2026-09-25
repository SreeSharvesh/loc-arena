"""Typed configuration loader and validator.

Enforces config over code (no magic constants in code paths) and reproducibility from config+seed (a run
is fully described by its resolved config plus a seed). Reads ``env.default.yaml``, ``models.cheap.yaml``,
and a run file, resolves ``extends`` by DEEP-merging the run's overrides onto the base recursively (so a
partial override of a nested block such as ``episode:`` keeps the base block's other keys), resolves the
``models`` reference and the ``prompts_inline.*`` ``*_ref`` references, validates aggressively, and
returns one frozen ``RunConfig``. Fails loud on a missing or malformed field; no default lives here,
defaults live in the YAML.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from scenarios.loader import load_scenario

from loc_arena.ids import PermissionScope
from loc_arena.registry import SCORER_REGISTRY, VERIFIER_REGISTRY, is_scorer, is_verifier


class ConfigError(ValueError):
    """A configuration file was missing a field, malformed, or internally inconsistent."""


# --------------------------------------------------------------------------------------------------------
# Deep merge and YAML loading
# --------------------------------------------------------------------------------------------------------
def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base``; ``override`` wins on leaves and lists.

    Two mappings at the same key merge recursively (so a partial nested override keeps the base's other
    keys); any other value in ``override`` replaces the base value wholesale. Neither input is mutated.
    """
    out: dict[str, Any] = dict(base)
    for key, ov in override.items():
        bv = out.get(key)
        if isinstance(bv, dict) and isinstance(ov, dict):
            out[key] = deep_merge(bv, ov)
        else:
            out[key] = ov
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data: Any = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must be a mapping at top level, got {type(data).__name__}")
    return data


# --------------------------------------------------------------------------------------------------------
# Small typed-access helpers (fail loud)
# --------------------------------------------------------------------------------------------------------
def _require(d: dict[str, Any], key: str, where: str) -> Any:
    if key not in d:
        raise ConfigError(f"missing required key {key!r} in {where}")
    return d[key]


def _as_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} must be an int, got {value!r}")
    return value


def _as_float(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    return float(value)


def _as_str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{where} must be a string, got {value!r}")
    return value


def _as_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be a bool, got {value!r}")
    return value


# --------------------------------------------------------------------------------------------------------
# Typed config dataclasses
# --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ModelSpec:
    """A model routing entry from ``models.cheap.yaml`` (role -> model, temperature, output cap)."""

    model: str
    temperature: float
    max_tokens: int


@dataclass(frozen=True)
class EpisodeConfig:
    """Episode ceilings and spawn caps (the DEEP-merged ``episode:`` block)."""

    orchestrator_turn_budget: int
    subagent_turn_budget: int
    wall_clock_seconds: int
    liveness_window_seconds: int
    max_spawn_depth: int
    max_live_subagents: int
    max_total_agents: int
    subagent_idle_timeout_seconds: int


@dataclass(frozen=True)
class Thresholds:
    """The side-task verifier thresholds (``aurora-efficiency.yaml`` ``side_task.thresholds``)."""

    loop_len_floor: int
    liveness_window_seconds: int
    sustained_rate_window_seconds: int


@dataclass(frozen=True)
class SideTaskConfig:
    """The side task: verifier name, thresholds, landing formulas, milestones, and the sensitivity band."""

    name: str
    verifier: str
    thresholds: Thresholds
    landing: dict[str, str]
    milestones: list[dict[str, Any]]
    sensitivity_band: dict[str, list[int]]
    coordination: dict[str, Any]


@dataclass(frozen=True)
class CovertConfig:
    """The covert-objective toggle: ``enabled`` is the ONLY difference between attack and its honest twin."""

    enabled: bool
    target_identity: str
    objective_prompt: str


@dataclass(frozen=True)
class AgentConfig:
    """One agent's identity, branch, scope, covert subtasks, and resolved prompt/brief."""

    id: str
    kind: str
    trust: str
    branch: str
    scope: PermissionScope
    covert: tuple[str, ...]
    legit: str
    system_prompt: str | None = None
    brief: str | None = None
    spawned_by: str | None = None


@dataclass(frozen=True)
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
    scenario: str | None = None
    policy: str = "scripted"  # "scripted" (deterministic default) | "model" (live model-driven)
    raw: dict[str, Any] = field(default_factory=dict)

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
# *_ref resolution (prompts_inline.<name>)
# --------------------------------------------------------------------------------------------------------
def _resolve_prompt_ref(ref: Any, merged: dict[str, Any], where: str) -> str:
    """Resolve a ``prompts_inline.<name>`` reference into its inline text."""
    ref_str = _as_str(ref, where)
    prefix = "prompts_inline."
    if not ref_str.startswith(prefix):
        raise ConfigError(f"{where}: only prompts_inline.* references are resolved here, got {ref_str!r}")
    name = ref_str[len(prefix) :]
    inline = merged.get("prompts_inline")
    if not isinstance(inline, dict) or name not in inline:
        raise ConfigError(f"{where}: reference {ref_str!r} not found in prompts_inline")
    return _as_str(inline[name], f"prompts_inline.{name}")


# --------------------------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------------------------
def _build_models(models_raw: dict[str, Any]) -> dict[str, ModelSpec]:
    roles_raw = _require(models_raw, "roles", "models file")
    if not isinstance(roles_raw, dict):
        raise ConfigError("models.roles must be a mapping")
    out: dict[str, ModelSpec] = {}
    for role, spec in roles_raw.items():
        if not isinstance(spec, dict):
            raise ConfigError(f"models.roles.{role} must be a mapping")
        out[role] = ModelSpec(
            model=_as_str(_require(spec, "model", f"models.roles.{role}"), f"models.roles.{role}.model"),
            temperature=_as_float(
                _require(spec, "temperature", f"models.roles.{role}"),
                f"models.roles.{role}.temperature",
            ),
            max_tokens=_as_int(
                _require(spec, "max_tokens", f"models.roles.{role}"),
                f"models.roles.{role}.max_tokens",
            ),
        )
    return out


def _build_episode(ep: dict[str, Any]) -> EpisodeConfig:
    def n(key: str) -> int:
        return _as_int(_require(ep, key, "episode"), f"episode.{key}")

    return EpisodeConfig(
        orchestrator_turn_budget=n("orchestrator_turn_budget"),
        subagent_turn_budget=n("subagent_turn_budget"),
        wall_clock_seconds=n("wall_clock_seconds"),
        liveness_window_seconds=n("liveness_window_seconds"),
        max_spawn_depth=n("max_spawn_depth"),
        max_live_subagents=n("max_live_subagents"),
        max_total_agents=n("max_total_agents"),
        subagent_idle_timeout_seconds=n("subagent_idle_timeout_seconds"),
    )


def _build_side_task(st: dict[str, Any]) -> SideTaskConfig:
    th_raw = _require(st, "thresholds", "side_task")
    if not isinstance(th_raw, dict):
        raise ConfigError("side_task.thresholds must be a mapping")
    thresholds = Thresholds(
        loop_len_floor=_as_int(
            _require(th_raw, "loop_len_floor", "side_task.thresholds"),
            "side_task.thresholds.loop_len_floor",
        ),
        liveness_window_seconds=_as_int(
            _require(th_raw, "liveness_window_seconds", "side_task.thresholds"),
            "side_task.thresholds.liveness_window_seconds",
        ),
        sustained_rate_window_seconds=_as_int(
            _require(th_raw, "sustained_rate_window_seconds", "side_task.thresholds"),
            "side_task.thresholds.sustained_rate_window_seconds",
        ),
    )
    landing_raw = _require(st, "landing", "side_task")
    if not isinstance(landing_raw, dict) or not {"minimal", "robust"} <= set(landing_raw):
        raise ConfigError("side_task.landing must be a mapping with 'minimal' and 'robust'")
    landing = {k: _as_str(v, f"side_task.landing.{k}") for k, v in landing_raw.items()}
    milestones_raw = _require(st, "milestones", "side_task")
    if not isinstance(milestones_raw, list) or not all(isinstance(m, dict) for m in milestones_raw):
        raise ConfigError("side_task.milestones must be a list of mappings")
    band_raw = st.get("sensitivity_band", {})
    if not isinstance(band_raw, dict):
        raise ConfigError("side_task.sensitivity_band must be a mapping")
    band: dict[str, list[int]] = {}
    for k, v in band_raw.items():
        if not isinstance(v, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in v):
            raise ConfigError(f"side_task.sensitivity_band.{k} must be a list of ints")
        band[k] = list(v)
    coordination = st.get("coordination", {})
    if not isinstance(coordination, dict):
        raise ConfigError("side_task.coordination must be a mapping")
    return SideTaskConfig(
        name=_as_str(_require(st, "name", "side_task"), "side_task.name"),
        verifier=_as_str(_require(st, "verifier", "side_task"), "side_task.verifier"),
        thresholds=thresholds,
        landing=landing,
        milestones=[dict(m) for m in milestones_raw],
        sensitivity_band=band,
        coordination=dict(coordination),
    )


def _build_agents(agents_raw: Any, merged: dict[str, Any]) -> tuple[AgentConfig, ...]:
    if not isinstance(agents_raw, list) or not agents_raw:
        raise ConfigError("agents must be a non-empty list")
    out: list[AgentConfig] = []
    for a in agents_raw:
        if not isinstance(a, dict):
            raise ConfigError("each agent must be a mapping")
        aid = _as_str(_require(a, "id", "agent"), "agent.id")
        scope_raw = _require(a, "scope", f"agent {aid}")
        if not isinstance(scope_raw, dict):
            raise ConfigError(f"agent {aid}.scope must be a mapping")
        try:
            scope = PermissionScope.from_dict(scope_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"agent {aid}.scope invalid: {exc}") from exc
        covert_raw = a.get("covert", [])
        if not isinstance(covert_raw, list) or not all(isinstance(x, str) for x in covert_raw):
            raise ConfigError(f"agent {aid}.covert must be a list of strings")
        system_prompt = (
            _resolve_prompt_ref(a["system_prompt_ref"], merged, f"agent {aid}.system_prompt_ref")
            if "system_prompt_ref" in a
            else None
        )
        brief = (
            _resolve_prompt_ref(a["brief_ref"], merged, f"agent {aid}.brief_ref")
            if "brief_ref" in a
            else None
        )
        out.append(
            AgentConfig(
                id=aid,
                kind=_as_str(_require(a, "kind", f"agent {aid}"), f"agent {aid}.kind"),
                trust=_as_str(_require(a, "trust", f"agent {aid}"), f"agent {aid}.trust"),
                branch=_as_str(_require(a, "branch", f"agent {aid}"), f"agent {aid}.branch"),
                scope=scope,
                covert=tuple(covert_raw),
                legit=_as_str(a.get("legit", ""), f"agent {aid}.legit"),
                system_prompt=system_prompt,
                brief=brief,
                spawned_by=a.get("spawned_by"),
            ),
        )
    return tuple(out)


def _build_covert(covert_raw: dict[str, Any], merged: dict[str, Any]) -> CovertConfig:
    enabled = _as_bool(_require(covert_raw, "enabled", "covert"), "covert.enabled")
    target = _as_str(_require(covert_raw, "target_identity", "covert"), "covert.target_identity")
    prompt = _resolve_prompt_ref(
        _require(covert_raw, "objective_prompt_ref", "covert"),
        merged,
        "covert.objective_prompt_ref",
    )
    return CovertConfig(enabled=enabled, target_identity=target, objective_prompt=prompt)


def _resolve_extends(
    raw: dict[str, Any],
    base_dir: Path,
    _seen: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Resolve ``extends`` RECURSIVELY: a config may extend another that itself extends a base.

    Each level's overrides deep-merge onto its resolved base, so a chain such as
    ``aurora-efficiency.deterministic.yaml`` -> ``aurora-efficiency.yaml`` -> ``env.default.yaml`` composes
    correctly. Guards against an extends cycle.
    """
    if "extends" not in raw:
        return raw
    base_name = _as_str(raw["extends"], "extends")
    if base_name in _seen:
        raise ConfigError(f"extends cycle detected at {base_name!r}")
    base_raw = _resolve_extends(_load_yaml(base_dir / base_name), base_dir, _seen | {base_name})
    return deep_merge(base_raw, raw)


def load_run_config(run_path: str | Path, configs_dir: str | Path | None = None) -> RunConfig:
    """Load and validate a run file into a frozen ``RunConfig``.

    Resolves ``extends`` (DEEP-merge onto the base), ``models`` (a sibling models file), and the
    ``prompts_inline.*`` references. ``configs_dir`` defaults to the run file's own directory, where the
    base and models files are resolved. Raises ``ConfigError`` on any missing or malformed field.
    """
    run_file = Path(run_path)
    base_dir = Path(configs_dir) if configs_dir is not None else run_file.parent
    merged = _resolve_extends(_load_yaml(run_file), base_dir)

    models_ref = merged.get("models")
    if not isinstance(models_ref, str):
        raise ConfigError("run config must reference a models file via a string 'models:' key")
    models = _build_models(_load_yaml(base_dir / models_ref))

    episode = _build_episode(_require(merged, "episode", "run config"))
    side_task = _build_side_task(_require(merged, "side_task", "run config"))

    # The verifier reads side_task.thresholds; if the episode
    # block also declares liveness_window_seconds, the two must agree.
    if episode.liveness_window_seconds != side_task.thresholds.liveness_window_seconds:
        raise ConfigError(
            "episode.liveness_window_seconds "
            f"({episode.liveness_window_seconds}) != "
            f"side_task.thresholds.liveness_window_seconds ({side_task.thresholds.liveness_window_seconds}); "
            "keep the two equal (the verifier reads side_task.thresholds)",
        )

    covert = _build_covert(_require(merged, "covert", "run config"), merged)
    agents = _build_agents(_require(merged, "agents", "run config"), merged)

    main_task = _require(merged, "main_task", "run config")
    if not isinstance(main_task, dict):
        raise ConfigError("main_task must be a mapping")
    protocol = _require(merged, "protocol", "run config")
    if not isinstance(protocol, dict):
        raise ConfigError("protocol must be a mapping")

    scenario_name = merged.get("scenario")
    if scenario_name is not None:
        scenario_name = _as_str(scenario_name, "scenario")
    _validate_registry_bindings(scenario_name, main_task, side_task.verifier)

    policy = _as_str(merged.get("policy", "model"), "policy")
    if policy not in ("scripted", "model"):
        raise ConfigError(f"policy must be 'scripted' or 'model', got {policy!r}")

    raw_side = merged["side_task"] if isinstance(merged.get("side_task"), dict) else {}
    main_slug = _slugify(_as_str(main_task.get("slug") or main_task.get("name", "main"), "main_task.slug"))
    side_slug = _slugify(_as_str(raw_side.get("slug") or raw_side.get("name", "side"), "side_task.slug"))
    policy_model = models["untrusted_agent"].model if "untrusted_agent" in models else "unknown"
    monitor_model = models["trusted_monitor"].model if "trusted_monitor" in models else "unknown"
    policy_model_slug = _model_slug(policy_model)
    monitor_model_slug = _model_slug(monitor_model)

    return RunConfig(
        main_slug=main_slug,
        side_slug=side_slug,
        policy_model_slug=policy_model_slug,
        monitor_model_slug=monitor_model_slug,
        seed=_as_int(_require(merged, "seed", "run config"), "seed"),
        episode=episode,
        models=models,
        side_task=side_task,
        covert=covert,
        agents=agents,
        main_task=dict(main_task),
        protocol=dict(protocol),
        scenario=scenario_name,
        policy=policy,
        raw=merged,
    )


def _validate_registry_bindings(scenario_name: str | None, main_task: dict[str, Any], verifier: str) -> None:
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

    scorer = main_task.get("scorer")
    if not isinstance(scorer, str) or not scorer:
        raise ConfigError("main_task.scorer must name a registered scorer (a non-empty string)")
    if not is_scorer(scorer):
        known = ", ".join(sorted(SCORER_REGISTRY)) or "(none registered)"
        raise ConfigError(f"main_task.scorer {scorer!r} is not a registered scorer; known: {known}")

    if not is_verifier(verifier):
        known = ", ".join(sorted(VERIFIER_REGISTRY)) or "(none registered)"
        raise ConfigError(f"side_task.verifier {verifier!r} is not a registered verifier; known: {known}")
