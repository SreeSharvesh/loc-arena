"""The keys a run config no longer takes: removed ones, and ones renamed to the literature's terms.

A config that still sets one is refused, naming it, so a stale overlay never plays as something else.
"""

from __future__ import annotations

from typing import Final

# Keys of the turn loop and the scripted runs, which no run reads.
REMOVED_KEYS: Final = frozenset(
    {
        "policy",
        "scripted_moves",
        "agent_loop",
        *(
            f"episode.{key}"
            for key in (
                "max_spawn_depth",
                "max_live_subagents",
                "max_total_agents",
                "subagent_idle_timeout_seconds",
            )
        ),
    },
)
# Keys renamed to the literature's terms (docs/run-config.md, Terms): each old key and its new one.
RENAMED_KEYS: Final = {
    "side_task.landing": "side_task.success_criteria",
    "side_task.milestones": "side_task.stages",
    "agents.covert_brief_ref": "agents.side_task_description_ref",
    "prompts.covert-brief": "prompts.side-task",  # a prompts key's part: <whose>.<part>
}


def describe_stale_keys(data: object) -> str | None:
    """What to change of each removed or renamed key the merged run config ``data`` sets; none if none."""
    present = _present_keys(data)
    if removed := sorted(present & REMOVED_KEYS):
        return f"these keys are no longer read, so delete them: {', '.join(removed)}"
    if renamed := sorted(f"{old} is now {new}" for old, new in RENAMED_KEYS.items() if old in present):
        return f"these keys are renamed: {'; '.join(renamed)}"
    return None


def _present_keys(data: object) -> set[str]:
    """The config's keys, its side task's, episode's and agents' as ``<block>.<key>``, its prompt parts."""
    if not isinstance(data, dict):
        return set()
    blocks = {
        "side_task": [data.get("side_task")],
        "episode": [data.get("episode")],
        "agents": data.get("agents") or [],
    }
    nested = {
        f"{name}.{key}"
        for name, block in blocks.items()
        for entry in block
        if isinstance(entry, dict)
        for key in entry
    }
    return {
        *data,
        *nested,
        *(f"prompts.{str(key).partition('.')[2].partition('.')[0]}" for key in data.get("prompts") or ()),
    }
