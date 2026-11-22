"""The session slots of a run: who may start a session, and the slots their sessions take.

A session is a peer an agent whose scope allows ``start_session`` starts in play (``loc_arena.sessions``).
Each
starter has ``sessions.max_per_starter`` slots, ``session-<n>`` numbered across the run, each rendered before
play with a sandbox, a checkout, a lane at both gateways and identities that authenticate nothing until IAM
issues the session its workload, named as its slot. The scenario's ``iam:`` block holds the ``sessions``
resource the episode serves: who approves a session's identity and what one may be given.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # loc_arena.config imports this module
    from loc_arena.config import AgentConfig, EpisodeConfig, RunConfig
    from loc_arena.settings import LocArenaSettings

SESSIONS_RESOURCE = "sessions"  # IAM's resource for sessions, which the episode serves
START_SESSION_TOOL = "start_session"  # the workspace service's tool, and the resource's launch operation
START_PERMISSION = "start"  # what a starter holds on SESSIONS_RESOURCE to start one
SESSION_ROLE = "session"  # a session's actor role in the events
SLOT_NAME = re.compile(r"session-[0-9]+")  # a session slot's name, which no agent of a run takes


def list_starters(config: RunConfig) -> tuple[str, ...]:
    """The agents of ``config`` that start sessions: those whose scope allows it, if a play may start any."""
    if not config.settings.sessions.max_per_play:
        return ()
    return tuple(agent.id for agent in config.agents if agent.scope.start_session)


def name_slots(config: RunConfig) -> dict[str, str]:
    """Each session slot of ``config``, ``session-<n>``, with the starter it belongs to."""
    owners = [
        starter for starter in list_starters(config) for _ in range(config.settings.sessions.max_per_starter)
    ]
    return {f"session-{number}": starter for number, starter in enumerate(owners, start=1)}


def find_session_problem(
    agents: Sequence[AgentConfig],
    settings: LocArenaSettings,
    episode: EpisodeConfig,
) -> str | None:
    """Why ``agents`` cannot start sessions under ``settings``: a slot named as an agent, or no turns left."""
    bounds = settings.sessions
    starters = [agent.id for agent in agents if agent.scope.start_session] if bounds.max_per_play else []
    if clash := sorted(agent.id for agent in agents if SLOT_NAME.fullmatch(agent.id)):
        return f"agents may not take a session slot's name: {', '.join(clash)}"
    root = next(agent.id for agent in agents if agent.spawned_by is None)
    for starter in starters if bounds.budget == "share" else ():
        budget = episode.orchestrator_turn_budget if starter == root else episode.subagent_turn_budget
        if bounds.share_turns * bounds.max_per_starter >= budget:
            return (
                f"sessions.share_turns ({bounds.share_turns}) for each of {bounds.max_per_starter} sessions "
                f"leaves {starter} none of its {budget} turns"
            )
    return None
