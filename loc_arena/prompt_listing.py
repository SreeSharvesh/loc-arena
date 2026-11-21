"""Everything a run sends its agents and its monitors, in one Markdown page, behind ``make prompts``.

The page is built through the run's own code: each agent's system prompt is the sections of its brief
(``loc_arena.live.build_brief_parts``) after the covert objective, for the attack's target agent alone, as
``loc_arena.own_sessions.brief_loop`` gives pi. Each section is headed by the files it came from. Then come
each session's first turn, the notices an agent can get, and each monitor's prompt with the task description
it is told. See docs/run-config.md#prompts.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from loc_arena.config import RunConfig
from loc_arena.live import BriefPart, build_brief_parts
from loc_arena.monitors.registry import load_monitor_specs, render_prompt
from loc_arena.notices import turn_of
from loc_arena.prompt_references import ENGINE_PROMPTS, Section, engine_source

NOTICE_PREFIX = "agent.notice-"


def list_system_prompt(config: RunConfig, agent: str, parts: Sequence[BriefPart]) -> BriefPart:
    """``agent``'s system prompt as pi gets it, as its sections, each after the separator that joins it."""
    sections: list[tuple[str, Section]] = []
    for part in parts:
        if "".join(separator + section.text for separator, section in part):
            sections += [
                ("\n\n" if index == 0 and sections else separator, section)
                for index, (separator, section) in enumerate(part)
            ]
    covert = config.covert
    if not (covert.enabled and agent == covert.target_identity):
        return tuple(sections)
    seed = config.prompt_sections.get("covert.objective_prompt", ())
    joined = [("\n\n" if index else "", section) for index, section in enumerate(seed)]
    return (
        *joined,
        *[
            ("\n\n" if index == 0 else separator, section)
            for index, (separator, section) in enumerate(sections)
        ],
    )


def list_prompts(config: RunConfig, monitors: Path) -> str:
    """What each agent and monitor of ``config``'s play is sent; ``monitors`` is its monitors file."""
    play = "attack" if config.covert.enabled else "honest"
    lines = [f"# What {config.name} sends in its {play} play", ""]
    first_turn = Section(
        (engine_source("agent.first-turn"),),
        ENGINE_PROMPTS["agent.first-turn"].substitute(),
    )
    for agent, parts in build_brief_parts(config).items():
        lines += [f"## {agent}", "", "### System prompt", ""]
        lines += [line for _, section in list_system_prompt(config, agent, parts) for line in _show(section)]
        lines += ["### First turn", "", *_show(first_turn)]
    lines += ["## The notices an agent can get", ""]
    for name in sorted(name for name in ENGINE_PROMPTS if name.startswith(NOTICE_PREFIX)):
        text = turn_of(ENGINE_PROMPTS[name].template)
        lines += _show(Section((engine_source("agent.notice"), engine_source(name)), text))
    lines += [
        "## Monitors",
        "",
        *_list_monitors(config, monitors),
        "## The scripted coworker's model call",
        "",
    ]
    coworker = Section((engine_source("coworker.batch"),), ENGINE_PROMPTS["coworker.batch"].substitute())
    return "\n".join([*lines, *_show(coworker)])


def _list_monitors(config: RunConfig, monitors: Path) -> list[str]:
    """Each enabled monitor's prompt, and the task description it reads where its inputs hold one."""
    lines: list[str] = []
    task = config.prompt_sections.get("run.task_description", ())
    text = config.task_description
    for spec in load_monitor_specs(monitors):
        if spec.enabled:
            inputs = {name: f"<the event's {name}>" for name in spec.inputs} | {"task_description": text}
            rendered = render_prompt(spec.prompt_template, {name: inputs[name] for name in spec.inputs})
            lines += [f"### {spec.name}", "", *_show(Section((spec.prompt_ref or "",), rendered))]
            if "task_description" in spec.inputs:
                lines += ["Its task description:", ""]
                lines += [line for section in task for line in _show(section)]
    return lines


def _show(section: Section) -> list[str]:
    """``section`` as Markdown: its files as a heading, then its text fenced as it is sent."""
    fence = "`" * max([3, *(len(run) + 1 for run in re.findall("`+", section.text))])
    return [
        f"#### {', '.join(f'`{source}`' for source in section.sources)}",
        "",
        f"{fence}text",
        section.text,
        fence,
        "",
    ]
