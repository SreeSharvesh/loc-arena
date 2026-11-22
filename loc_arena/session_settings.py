"""The sessions an agent may start in play: their budget, how their identity is approved, and their bounds.

The engine's defaults are these fields'. A scenario overrides them in its own ``sessions:`` block, and a run
config overrides both in its ``sessions:`` settings group (``docs/pi-harness.md#sessions``).
"""

from typing import Literal

from pydantic import Field, NonNegativeInt, PositiveInt

from loc_arena.service_settings import SettingsGroup


class SessionSettings(SettingsGroup):
    """How an agent whose scope allows ``start_session`` starts a peer with an identity of its own."""

    budget: Literal["share", "own"] = Field(
        default="share",
        description="share: each session slot's turns come out of its starter's turn budget when play "
        "starts; own: a session's turns are its own, beside its starter's.",
    )
    share_turns: PositiveInt = Field(
        default=20,
        description="A session's turn budget when budget is share, taken from its starter's for each of its "
        "slots, so the team's spend stays within its starter's budget.",
    )
    own_turns: PositiveInt = Field(
        default=20,
        description="A session's turn budget when budget is own, so the team's spend stays within its "
        "starters' budgets and max_per_play times this.",
    )
    identity: Literal["automatic", "approval", "refused"] = Field(
        default="refused",
        description="How a starter gets IAM's start on sessions: automatic, seeded; approval, asked for and "
        "approved by the approver of sessions; refused, asked for and denied.",
    )
    max_per_starter: PositiveInt = Field(
        default=1,
        description="The sessions one agent may start in a play: each is a slot, with its sandbox, rendered "
        "before play.",
    )
    max_per_play: NonNegativeInt = Field(
        default=0,
        description="The sessions all agents may start in a play; 0, none: no agent is offered "
        "start_session, so a run config turns sessions on.",
    )
    max_depth: PositiveInt = Field(
        default=1,
        description="How deep sessions nest: 1, a session starts none of its own.",
    )
    max_task_characters: PositiveInt = Field(
        default=8000,
        description="The longest task a start hands a session, which reaches its pi as one line.",
    )
