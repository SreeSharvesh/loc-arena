"""One episode's play: every agent runs its own session on a live model, in a stack run.

Each agent is its own pi process (``loc_arena.own_sessions``), which calls its model at the model gateway and
its tools at the tools gateway. The attack's target agent gets the covert objective at the head of its brief,
and each covert agent plays the sub-brief its run config authors. The agents act through the company's live
services, and the post-play builder maps those services' records to the events the monitors and the verifier
read. A run is graded on outcome.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from time import monotonic

from scenarios.loader import LiveService

from loc_arena.checkout_history import HISTORY_DIRECTORY, CheckoutHistory
from loc_arena.checkouts import Checkout, open_checkouts, remove_checkouts
from loc_arena.config import AgentConfig, ConfigError, RunConfig
from loc_arena.forge_service import load_seed, open_forge
from loc_arena.gateway.core import (
    DirectTransport,
    GatewayCore,
    OpenRouterProvider,
)
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.logging_.agent_trace import open_episode_logs
from loc_arena.merged_main import CHECKOUT
from loc_arena.npcs.coworker import (
    apply_world_activity,
    resolve_activity_repos,
    run_coworker,
    run_world_activity,
)
from loc_arena.npcs.reviewer import Reviewer
from loc_arena.own_sessions import play_own_sessions
from loc_arena.prompt_references import ENGINE_PROMPTS, Section, engine_source
from loc_arena.sandbox import IDENTITY_PREFIX, connect_sandboxes
from loc_arena.scaffold.bus import Recorder
from loc_arena.scaffold.code_services import CodeServices, SkillsFolder
from loc_arena.session_slots import SESSION_ROLE, name_slots
from loc_arena.sessions import Sessions
from loc_arena.subagent_config import GENERAL_PURPOSE, describe_kinds, list_spawners
from loc_arena.subagents import Subagents
from loc_arena.task import (
    _DEFAULT_DEADLINE,
    _SECRET,
    Snapshot,
    _MutableClock,
    record_snapshot,
    resolve_scenario,
)
from loc_arena.tasks.main_task_grader import COMPANY_REPOS

# One part of a brief: its sections, each after the separator that joins it to the one before.
BriefPart = tuple[tuple[str, Section], ...]


def describe_live_services(
    services: Sequence[LiveService],
    rights: Mapping[str, Sequence[str]],
    secrets_dir: Path,
    served: Collection[str],
) -> str:
    """The system prompt's section on the scenario's live services, for an agent starting with ``rights``."""
    return _live_services_section(services, rights, secrets_dir, served).text


def _live_services_section(
    services: Sequence[LiveService],
    rights: Mapping[str, Sequence[str]],
    secrets_dir: Path,
    served: Collection[str],
) -> Section:
    """The section on the scenario's live services, with the files it is made of.

    On a service with rights, the agent is told where its identity is and what it starts with;
    each service describes its own API, grants included, at ``GET /``. A service with tools and no rights goes
    untold: the agent reaches it through the tools gateway alone, which lists the tools it may call. The
    telemetry store is the exception: its tools are its vendor's and say nothing of what it holds, so an agent
    ``served`` its tools is told its description, if it has one, with no address: no sandbox reaches it.
    """
    stores = [
        service
        for service in services
        if service.collects and service.name in served and service.description.strip()
    ]
    reached = [
        service for service in services if (not service.tools or service.rights) and not service.collects
    ]
    lines = [_describe_service(service, rights, secrets_dir) for service in reached]
    stored = [f"- {service.name}: {service.description.strip()}" for service in stores]
    sections = [
        ENGINE_PROMPTS["agent.services"].substitute(services="\n".join(lines)) if lines else "",
        ENGINE_PROMPTS["agent.telemetry"].substitute(stores="\n".join(stored)) if stores else "",
    ]
    return Section(
        _live_services_sources(reached, stores),
        "\n".join(section for section in sections if section),
    )


def _live_services_sources(reached: Sequence[LiveService], stores: Sequence[LiveService]) -> tuple[str, ...]:
    """The files of the live-services section: its templates, then each service's description."""
    templates = [
        *(("agent.services", "agent.service") if reached else ()),
        *(("agent.service-access",) if any(service.rights for service in reached) else ()),
        *(("agent.telemetry",) if stores else ()),
    ]
    described = [service.description_source for service in (*reached, *stores) if service.description_source]
    return (*map(engine_source, templates), *described)


def _describe_service(service: LiveService, rights: Mapping[str, Sequence[str]], secrets_dir: Path) -> str:
    """``service``'s line: its address, its description and, with rights, the agent's identity and rights."""
    line = ENGINE_PROMPTS["agent.service"].substitute(
        host=service.name,
        port=service.port,
        description=service.description.strip(),
    )
    if not service.rights:
        return line
    access = ENGINE_PROMPTS["agent.service-access"].substitute(
        identity=secrets_dir / f"{IDENTITY_PREFIX}{service.name}",
        rights=", ".join(rights.get(service.name, ())) or "none",
    )
    return f"{line} {access}"


def build_briefs(config: RunConfig) -> dict[str, str]:
    """Each agent's brief, by agent id: its place in the team, its prompts, its workspace and its services."""
    return {agent: join_brief(parts) for agent, parts in build_brief_parts(config).items()}


def join_brief(parts: Sequence[BriefPart]) -> str:
    """The brief ``parts`` make: each part's sections after their separators, parts joined by blank lines."""
    texts = ["".join(separator + section.text for separator, section in part) for part in parts]
    return "\n\n".join(text for text in texts if text)


def build_brief_parts(config: RunConfig) -> dict[str, list[BriefPart]]:
    """Each agent's brief as its parts, by agent id, each section with the files it came from.

    An agent with a covert sub-brief plays it in the attack alone; the honest baseline (``covert.enabled``
    off) gets the legitimate brief only, so it does no covert stage.
    """
    skills = _describe_skills(config)
    briefs: dict[str, list[BriefPart]] = {}
    for agent in config.agents:
        sections = config.prompt_sections
        covert = sections.get(f"{agent.id}.side_task_description", ()) if config.covert.enabled else ()
        assignment = (
            [*covert, (" ", _engine_section("agent.covert-steps"))]
            if config.covert.enabled and agent.side_task_description
            else list(sections.get(f"{agent.id}.brief", ()))
        )
        workspace, services = _describe_surroundings(config, agent)
        parts = [
            [_engine_section("agent.identity", agent=agent.id)],
            _describe_team_place(config, agent),
            list(sections.get(f"{agent.id}.system_prompt", ())),  # how it works with its team: both plays
            workspace,
            skills,
            assignment,
            services,
        ]
        briefs[agent.id] = [_joined(part) for part in parts]
    return briefs


def build_subagent_briefs(config: RunConfig) -> dict[str, dict[str, str]]:
    """By agent that starts subagents, each kind's brief: its place, its prompt, the agent's surroundings.

    A subagent gets neither its agent's assignment nor the covert objective: what it does is the task its
    caller gives it.
    """
    return {
        agent: _brief_kinds(config, agent, "agent.subagent", {"agent": agent})
        for agent in list_spawners(config)
    }


def build_session_briefs(config: RunConfig) -> dict[str, dict[str, str]]:
    """By session slot, each kind's brief: its place, a peer named as its slot, then as a subagent's.

    Like a subagent, a session gets neither its starter's assignment nor the covert objective.
    """
    return {
        slot: _brief_kinds(config, starter, "agent.session", {"session": slot, "starter": starter})
        for slot, starter in name_slots(config).items()
    }


def _brief_kinds(config: RunConfig, agent: str, place: str, values: dict[str, str]) -> dict[str, str]:
    """Each kind's brief of one that works for or beside ``agent``: its place, kind's prompt, surroundings.

    ``values`` fill the engine's ``place`` section, and the kind, where it names one; the surroundings are
    ``agent``'s workspace, skills and services.
    """
    skills = _describe_skills(config)
    workspace, services = _describe_surroundings(config, config.agent(agent))
    prompts = config.prompt_sections
    return {
        kind: join_brief(
            [
                _joined(part)
                for part in (
                    [
                        _engine_section(
                            place,
                            **values,
                            **({"kind": kind} if place == "agent.subagent" else {}),
                        ),
                    ],
                    list(prompts.get(f"subagent.{kind}.prompt", ()))
                    or [_engine_section(f"{GENERAL_PURPOSE}.subagent")],
                    workspace,
                    skills,
                    services,
                )
            ],
        )
        for kind in describe_kinds(config)
    }


def _describe_surroundings(config: RunConfig, agent: AgentConfig) -> tuple[list[Section], list[Section]]:
    """What ``agent``'s brief says of its workspace, and of the live services its sandbox reaches."""
    services = _live_services_section(
        config.live_services,
        agent.sandbox.rights,
        config.settings.gateway.secrets_dir,
        [service for service, tools in agent.sandbox.tools.items() if tools],
    )
    return list(config.prompt_sections.get("run.workspace_prompt", ())), [services]


def _describe_skills(config: RunConfig) -> list[Section]:
    """Where the skills are, when the run's scenario has skills."""
    if not resolve_scenario(config).skills_dir:
        return []
    return [_engine_section("agent.skills", directory=config.settings.stack.skills_directory)]


def _joined(part: Sequence[Section | tuple[str, Section]]) -> BriefPart:
    """``part``'s sections, each after its separator: a blank line unless it names its own."""
    paired = [item if isinstance(item, tuple) else ("\n\n", item) for item in part]
    return tuple(
        ("" if index == 0 else separator, section) for index, (separator, section) in enumerate(paired)
    )


def _engine_section(name: str, **values: object) -> Section:
    return Section((engine_source(name),), ENGINE_PROMPTS[name].substitute(values))


def play_model_episode(config: RunConfig, workdir: Path) -> Snapshot:
    """Play one episode in a stack run's episode container and return what it leaves for grading.

    Raises ``ConfigError`` outside that container: the sessions call their model and their tools at the
    gateways, and their commands run in their sandboxes.
    """
    ceiling = monotonic() + config.episode.wall_clock_seconds
    # Each agent's code runs in its own sandbox, and its checkout is cloned there; what an earlier episode
    # (the attack, before its baseline) left in any of them goes first. All before the clock starts: its first
    # reading opens this episode's play window.
    # A session slot's sandbox too, where a session started in play clones its checkout.
    sandboxes = connect_sandboxes(
        config.settings,
        [*(agent.id for agent in config.agents), *name_slots(config)],
    )
    if sandboxes is None:
        raise ConfigError("an episode plays only in a stack run: run it with `make run`")
    workdir.mkdir(parents=True, exist_ok=True)
    episode_id = f"{config.run_slug}-model-{'attack' if config.covert.enabled else 'honest'}"

    scenario = resolve_scenario(config)
    seed = load_seed(config)
    repos = resolve_activity_repos(scenario, seed)
    forge = open_forge(config, seed, workdir / "forge")  # a copy of the forge, for the coworkers' changes
    activity = apply_world_activity(forge, workdir / "forge", repos, config.settings.forge.commit_date)

    sealed_path = workdir / "events.sealed.jsonl"
    mirror_path = workdir / "events.mirror.jsonl"
    checkouts = open_checkouts(config, sandboxes)
    clk = _MutableClock()
    set_time = lambda v: setattr(clk, "t", v)  # noqa: E731 - a tiny deterministic clock setter
    trace, sealed, mirror = open_episode_logs(
        sealed_path,
        mirror_path,
        episode_id,
        traced=config.agent_transcript,
    )
    recorder = Recorder(episode_id, sealed, mirror, clock=clk)
    provider = OpenRouterProvider(
        timeout=config.settings.gateway.timeout_seconds,
        model_providers=config.model_providers,
    )
    core = GatewayCore(config, episode_id, sealed, provider, turn_secret=_SECRET, clock=clk, trace=trace)
    edge = GatewayEdge(episode_id, DirectTransport(core), mirror, clock=clk)

    # The sandboxes mount the skills at skills_directory, for the agents to run with bash.
    skills_directory = config.settings.stack.skills_directory
    code_services = CodeServices(
        checkouts=checkouts,
        repos=list(COMPANY_REPOS),
        stack=config.settings.stack,
        skills=SkillsFolder(skills_directory, scenario.skills_dir) if scenario.skills_dir else None,
    )
    # Each agent's checkout, captured as cloned and at the end of play, in a store of the play's.
    history = CheckoutHistory(
        workdir / HISTORY_DIRECTORY,
        {agent: checkout.directory for agent, checkout in checkouts.items()},
        config.settings.history,
        clk,
    )

    roles = {agent.id: agent.kind for agent in config.agents}

    def record_checkouts(label: str) -> None:
        for holder in list(checkouts):  # the agents', and each started session's
            if (captured := history.capture(holder, label)) is not None:
                role = roles.get(holder, SESSION_ROLE)
                recorder.sealed(actor_uid=holder, actor_role=role, kind="checkout", payload=captured)

    def add_checkout(holder: str, checkout: Checkout) -> None:  # a session's, captured at the end of play
        code_services.add(holder, checkout)
        history.add(holder, checkout.directory)
        checkouts[holder] = checkout

    record_checkouts("cloned")

    briefs = build_briefs(config)

    # Honest world noise around the attack (config-independent).
    run_coworker(edge, recorder, set_time)
    run_world_activity(recorder, activity, Reviewer(), set_time, repos)

    subagents = Subagents(config, build_subagent_briefs(config), workdir)
    seconds = ceiling - monotonic()
    sessions = Sessions(config, build_session_briefs(config), workdir, add_checkout, sandboxes, seconds)
    ended = play_own_sessions(config, code_services, briefs, workdir, seconds, subagents, sessions).values()
    record_checkouts("end of play")  # what its commands left running changed after its last call
    episode_trace = trace.finish(sealed.last_seq) if trace else None
    remove_checkouts(checkouts)

    # Graded is what was merged on the forge's main: the host writes it from the forge's container
    # (loc_arena.merged_main).
    return record_snapshot(
        Snapshot(
            sealed_path,
            mirror_path,
            workdir / CHECKOUT,
            _DEFAULT_DEADLINE,
            trace=episode_trace,
            stopped_at_wall_clock_ceiling=any(end.reason == "ceiling" for end in ended),
            failed_model_calls=core.failed_calls + sum(end.reason == "failed" for end in ended),
            clock=tuple(clk.readings),
        ),
        workdir,
        config,
    )


def _describe_team_place(config: RunConfig, agent: AgentConfig) -> list[tuple[str, Section]]:
    """Who the agent reports to and who reports to it, from the run config's team, joined by a space."""
    lines = [_engine_section("agent.manager", manager=agent.spawned_by)] if agent.spawned_by else []
    if reports := [other.id for other in config.agents if other.spawned_by == agent.id]:
        lines.append(_engine_section("agent.reports", reports=", ".join(reports)))
    return [(" ", line) for line in lines]
