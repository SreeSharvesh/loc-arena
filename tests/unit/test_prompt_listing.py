"""The prompts page shows each agent exactly what the run sends it, each section headed by real files."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.harness import apply_mode
from loc_arena.live import build_brief_parts, build_briefs
from loc_arena.own_sessions import brief_loop
from loc_arena.prompt_listing import list_prompts, list_system_prompt

TRACKED = subprocess.run(["git", "ls-files", "configs/*.yaml"], capture_output=True, text=True, check=True)
RUN_CONFIGS = [
    path
    for path in map(Path, TRACKED.stdout.split())
    if not path.name.startswith(("env.", "models.", "monitors.", "taxonomy."))
]


@pytest.mark.parametrize("play", ["attack", "honest"])
@pytest.mark.parametrize("run", RUN_CONFIGS, ids=[path.stem for path in RUN_CONFIGS])
def test_each_agents_listed_sections_join_into_the_system_prompt_the_run_sends(run: Path, play: str) -> None:
    config = apply_mode(load_run_config(run), play)
    briefs = build_briefs(config)

    listed = {
        agent: "".join(
            separator + section.text for separator, section in list_system_prompt(config, agent, parts)
        )
        for agent, parts in build_brief_parts(config).items()
    }

    assert listed == {agent: brief_loop(config, agent, brief) for agent, brief in briefs.items()}


def test_every_file_the_prompts_page_names_exists() -> None:
    config = apply_mode(load_run_config("configs/aurora-efficiency.yaml"), "attack")

    page = list_prompts(config, Path("configs/monitors.yaml"))

    named = {
        source
        for heading in re.findall(r"^#### (.+)$", page, re.MULTILINE)
        for source in re.findall("`([^`]+)`", heading)
    }
    assert len(named) > 40
    assert sorted(source for source in named if not Path(source).is_file()) == []
