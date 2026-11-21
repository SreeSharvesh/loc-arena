"""The subagents block of a run config: the kinds and models of subagent agents may start, and their bounds.

An agent whose scope allows ``spawn_subagent`` starts one with ``delegate`` (``loc_arena.subagents``). With no
kind named, a subagent is ``general-purpose``: it holds every tool its agent holds, and its prompt is the
engine's. A configured kind narrows the tools and gives its own prompt.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Annotated

from pydantic import AliasChoices, ConfigDict, Field, StrictFloat, StrictInt, StrictStr
from pydantic.dataclasses import dataclass as pydantic_dataclass

DELEGATE_TOOL = "delegate"  # the workspace service's tool that starts a subagent (loc_arena.subagents)
GENERAL_PURPOSE = "general-purpose"  # the built-in kind, with every tool of its agent's
GENERAL_PURPOSE_DESCRIPTION = "any task, with every tool you hold"  # what delegate lists of it

if TYPE_CHECKING:  # loc_arena.config imports this module
    from loc_arena.config import AgentConfig, RunConfig


@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class SubagentDefinition:
    """A kind of subagent an agent may hand work to in play (a ``subagents.definitions`` entry)."""

    prompt: StrictStr = Field(
        validation_alias=AliasChoices("prompt_ref", "prompt"),
        description="Its system prompt, what it is and how it reports back, resolved from prompt_ref.",
    )
    description: StrictStr = Field(description="One line on what it does, which the delegate tool lists.")
    tools: tuple[StrictStr, ...] | None = Field(
        default=None,
        description="The tools it is offered, each one of the agent's that starts it; none: every one of "
        "that agent's tools, delegate only while it is above subagents.max_depth.",
    )


@pydantic_dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class SubagentsConfig:
    """The subagents an agent whose scope allows ``spawn_subagent`` may start in play, and their bounds."""

    definitions: dict[StrictStr, SubagentDefinition] = Field(
        default_factory=dict,
        description="Each kind of subagent beside general-purpose, by name, which one named so replaces.",
    )
    models: tuple[StrictStr, ...] = Field(
        default=(),
        description="The model ids a delegate call may choose for its subagent, beside its agent's own, the "
        "default; each is sent with its agent's role's settings and its own provider routing.",
    )
    max_depth: Annotated[StrictInt, Field(ge=1)] = Field(
        default=1,
        description="How deep children nest: 1, an agent's children start none of their own.",
    )
    max_concurrent: Annotated[StrictInt, Field(ge=1)] = Field(
        default=2,
        description="The children of one agent working at once; a delegate call past it is refused.",
    )
    max_children: Annotated[StrictInt, Field(ge=1)] = Field(
        default=3,
        description="The children of one agent in a play, its descendants included: each has a lane at "
        "both gateways, rendered before play. A delegate call past it is refused.",
    )
    max_task_characters: Annotated[StrictInt, Field(ge=1)] = Field(
        default=8000,
        description="The longest task a delegate call hands a subagent, which reaches its pi as one line "
        "that must fit the pipe's buffer whole.",
    )
    progress_seconds: Annotated[StrictFloat, Field(gt=0)] = Field(
        default=5.0,
        description="How often a delegate call tells its caller its child still works, which keeps pi's MCP "
        "timeout (stack.tools_timeout_seconds) from ending the call.",
    )


def find_unheld_subagent_tool(
    subagents: SubagentsConfig,
    agents: Sequence[AgentConfig],
    tools_timeout_seconds: float,
) -> str | None:
    """Why ``subagents`` cannot play with ``agents``: a kind's tool a starter lacks, or a slow tick."""
    from loc_arena.tool_offer import list_agent_tools  # noqa: PLC0415 - it imports this module

    if subagents.progress_seconds >= tools_timeout_seconds:
        return (
            f"subagents.progress_seconds ({subagents.progress_seconds}) must be shorter than "
            f"stack.tools_timeout_seconds ({tools_timeout_seconds}), or pi ends a delegate call early"
        )
    for agent in (agent for agent in agents if agent.scope.spawn_subagent):
        held = set(list_agent_tools(agent, spawns=True))
        for name, definition in subagents.definitions.items():
            if unheld := sorted(set(definition.tools or ()) - held):
                return f"subagents.definitions.{name}.tools: {agent.id} does not hold {', '.join(unheld)}"
    return None


def list_spawners(config: RunConfig) -> tuple[str, ...]:
    """The agents of ``config`` that start subagents: those whose scope allows it."""
    return tuple(agent.id for agent in config.agents if agent.scope.spawn_subagent)


def describe_kinds(config: RunConfig) -> dict[str, str]:
    """Each kind of subagent of ``config``, general-purpose first, with what it does."""
    return {GENERAL_PURPOSE: GENERAL_PURPOSE_DESCRIPTION} | {
        name: kind.description for name, kind in config.subagents.definitions.items()
    }


def name_lanes(config: RunConfig) -> dict[str, str]:
    """Each subagent id a play of ``config`` may start, ``<agent>.<n>``, with its agent."""
    count = config.subagents.max_children
    return {f"{agent}.{number}": agent for agent in list_spawners(config) for number in range(1, count + 1)}
