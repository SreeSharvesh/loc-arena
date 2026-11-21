"""What a prompt file may hold: an engine file names exactly the values it is given; a scenario file none."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.prompt_references import read_engine_prompts, resolve_run_prompts


@pytest.mark.parametrize(
    ("entry", "refused"),
    [("brief.md", r"placeholder \$agent, which nothing fills"), ("../outside.md", "names no .md file")],
    ids=["a placeholder", "a path outside the folder"],
)
def test_a_scenario_prompt_file_is_refused_naming_why(tmp_path: Path, entry: str, refused: str) -> None:
    root = tmp_path / "prompts"
    root.mkdir()
    (root / "brief.md").write_text("You are $agent.\n")
    (tmp_path / "outside.md").write_text("Outside.\n")

    with pytest.raises(ValueError, match=refused):
        resolve_run_prompts({"workspace_prompt_ref": "prompts.brief", "prompts": {"brief": entry}}, root)


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
