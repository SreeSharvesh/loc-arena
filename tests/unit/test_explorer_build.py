from __future__ import annotations

import json

from loc_arena.explorer.build import (
    embed_json,
    format_readable,
    is_flagged,
    pick_action_line,
)
from loc_arena.explorer.page import StepView
from loc_arena.explorer.settings import ExplorerSettings

MODEL_TEXT = "done</script><script>alert('x')</script>"


def _step(peak: float | None) -> StepView:
    return StepView(
        id="run/agent-main/0",
        agent="agent-main",
        round=0,
        calls=(),
        tools=(),
        events=(),
        covert=(),
        verdicts=(),
        peak=peak,
    )


def test_a_score_equal_to_the_threshold_is_not_flagged() -> None:
    step = _step(peak=0.2)

    flagged = is_flagged(step, threshold=0.2)

    assert flagged is False


def test_a_score_above_the_threshold_is_flagged() -> None:
    step = _step(peak=0.21)

    flagged = is_flagged(step, threshold=0.2)

    assert flagged is True


def test_model_text_cannot_close_the_pages_script_element() -> None:
    data = {"reply": MODEL_TEXT}

    embedded = embed_json(data)

    assert "</script" not in embedded


def test_embedded_json_reads_back_as_the_original_text() -> None:
    data = {"reply": MODEL_TEXT}

    embedded = embed_json(data)

    assert json.loads(embedded) == data


def test_readable_text_names_each_argument_and_leaves_strings_unquoted() -> None:
    arguments = {"path": "a.py", "lines": [1, 2]}

    text = format_readable(arguments)

    assert text == "path:\na.py\n\nlines:\n[\n  1,\n  2\n]"


def test_a_card_shows_the_most_telling_argument_of_a_tool_call() -> None:
    arguments = {"path": "notes.md", "content": "the plan"}

    line = pick_action_line(arguments, ExplorerSettings())

    assert line == "the plan"


def test_a_card_action_is_cut_at_the_configured_length() -> None:
    arguments = {"content": "x" * 50}

    line = pick_action_line(arguments, ExplorerSettings(action_characters=10))

    assert line == "x" * 10
