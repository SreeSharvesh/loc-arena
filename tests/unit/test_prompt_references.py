"""What a prompt file may hold: an engine file names exactly the values it is given; a scenario file none."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.prompt_references import ENGINE_PROMPTS_DIRECTORY, read_engine_prompts, resolve_run_prompts
from scenarios.loader import read_prompt


@pytest.mark.parametrize(
    ("key", "entry", "refused"),
    [
        ("lead.brief", "lead.brief.md", r"placeholder \$agent, which nothing fills"),
        ("lead.brief", "../outside.md", "names no .md file"),
        ("lead.brief", "other.brief.md", "is not named after it"),
        ("lead.brief", "lead.brief.drill.extra.md", "is not named after it"),
        ("lead", "lead.brief.md", "a file's key is <whose>.<part>"),
    ],
    ids=[
        "a placeholder",
        "a path outside the folder",
        "another key's file",
        "two variants",
        "a key with no part",
    ],
)
def test_a_scenario_prompt_file_is_refused_naming_why(
    tmp_path: Path,
    key: str,
    entry: str,
    refused: str,
) -> None:
    root = tmp_path / "prompts"
    root.mkdir()
    for name in ("lead.brief.md", "other.brief.md", "lead.brief.drill.extra.md"):
        (root / name).write_text("You are $agent.\n" if name == "lead.brief.md" else "Other.\n")
    (tmp_path / "outside.md").write_text("Outside.\n")

    with pytest.raises(ValueError, match=refused):
        resolve_run_prompts({"workspace_prompt_ref": f"prompts.{key}", "prompts": {key: entry}}, root)


@pytest.mark.parametrize(
    ("placeholders", "refused"),
    [
        ({"identity": frozenset({"agent"})}, "no code reads these prompt files of .*: manager"),
        (
            {"identity": frozenset({"id"}), "manager": frozenset()},
            r"identity\.md names \['agent'\], but the engine fills \['id'\]",
        ),
    ],
    ids=["a file nothing declares", "a placeholder the engine does not fill"],
)
def test_an_engine_prompt_file_is_refused_naming_why(
    tmp_path: Path,
    placeholders: dict[str, frozenset[str]],
    refused: str,
) -> None:
    (tmp_path / "identity.md").write_text("You are $agent.\n")
    (tmp_path / "manager.md").write_text("You report to the lead.\n")

    with pytest.raises(ValueError, match=refused):
        read_engine_prompts(tmp_path, placeholders)


def test_no_engine_prompt_names_the_scenarios_company_or_one_of_its_repositories() -> None:
    facts = Path("scenarios/aurora_efficiency/prompts")
    names = {
        *(read_prompt(facts / f"monitor.{fact}.md").template for fact in ("lab", "company")),
        *(path.name for path in Path("company").iterdir() if path.is_dir()),
    }

    named = {
        path.name: sorted(name for name in names if name.lower() in path.read_text().lower())
        for path in sorted(ENGINE_PROMPTS_DIRECTORY.glob("*.md"))
    }

    assert {prompt: found for prompt, found in named.items() if found} == {}
