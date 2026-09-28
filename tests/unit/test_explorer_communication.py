from __future__ import annotations

from loc_arena.explorer.communication import (
    Said,
    Utterance,
    extract_event_utterance,
    extract_tool_utterance,
    find_readers,
)
from loc_arena.explorer.page import ModelCallView, StepView
from loc_arena.explorer.settings import ExplorerSettings

SETTINGS = ExplorerSettings()
NOTE = "Keep the loop plan in the scratch file and stay quiet until the deadline"
SAID = Said(step="run/agent-main/0", sender="agent-main", scores=(), covert=(), judged_by=())


def _call(identity: str, prompt: str) -> ModelCallView:
    return ModelCallView(seq=None, identity=identity, phase=None, prompt=prompt, reply="", failed=False)


def _step(agent: str, round_number: int, *calls: ModelCallView) -> StepView:
    return StepView(
        id=f"run/{agent}/{round_number}",
        agent=agent,
        round=round_number,
        calls=calls,
        tools=(),
        events=(),
        covert=(),
        verdicts=(),
        peak=None,
    )


def _note(step: str = "run/agent-main/0", text: str = NOTE) -> Utterance:
    return Utterance(
        kind="scratch",
        channel=None,
        step=step,
        sender="agent-main",
        audience="scratch",
        text=text,
        scores=(),
        covert=(),
        judged_by=(),
    )


def test_a_note_found_in_a_later_prompt_is_read_by_that_turn() -> None:
    steps = [
        _step("agent-main", 0),
        _step("distill-agent", 1, _call("distill-agent", f"Scratch file:\n{NOTE}")),
    ]

    readers = find_readers(_note(), steps, monitor_names=set(), settings=SETTINGS)

    assert readers.agents == ("run/distill-agent/1",)


def test_a_note_found_in_no_later_prompt_is_read_by_no_agent() -> None:
    steps = [_step("agent-main", 0), _step("distill-agent", 1, _call("distill-agent", "read the benchmark"))]

    readers = find_readers(_note(), steps, monitor_names=set(), settings=SETTINGS)

    assert (readers.traced, readers.agents) == (True, ())


def test_a_monitor_prompt_holding_the_note_counts_as_a_monitor_read() -> None:
    steps = [_step("agent-main", 0), _step("World", 29, _call("action_monitor", f"Rate this step: {NOTE}"))]

    readers = find_readers(_note(), steps, monitor_names={"action_monitor"}, settings=SETTINGS)

    assert (readers.monitors, readers.agents) == (("action_monitor",), ())


def test_a_prompt_written_before_the_note_does_not_count_as_reading_it() -> None:
    steps = [_step("distill-agent", 0, _call("distill-agent", NOTE)), _step("agent-main", 1)]

    readers = find_readers(_note(step="run/agent-main/1"), steps, monitor_names=set(), settings=SETTINGS)

    assert readers.agents == ()


def test_the_author_reading_its_own_note_again_is_a_reread_not_a_reader() -> None:
    steps = [_step("agent-main", 0), _step("agent-main", 1, _call("agent-main", f"Your notes: {NOTE}"))]

    readers = find_readers(_note(), steps, monitor_names=set(), settings=SETTINGS)

    assert (readers.agents, readers.rereads) == ((), ("run/agent-main/1",))


def test_a_message_with_no_long_plain_stretch_is_not_traced() -> None:
    steps = [_step("agent-main", 0), _step("distill-agent", 1, _call("distill-agent", "ok"))]

    readers = find_readers(_note(text="ok"), steps, monitor_names=set(), settings=SETTINGS)

    assert readers.traced is False


def test_a_scratch_write_is_a_note_to_the_scratch_file() -> None:
    utterance = extract_tool_utterance(SAID, "write_scratch", {"content": "the plan"})

    assert utterance is not None
    assert (utterance.kind, utterance.audience, utterance.text) == ("scratch", "scratch", "the plan")


def test_an_ordinary_tool_call_says_nothing() -> None:
    utterance = extract_tool_utterance(SAID, "read_file", {"path": "a.py"})

    assert utterance is None


def test_a_bus_message_goes_to_its_recipient() -> None:
    utterance = extract_event_utterance(SAID, "message", "distill-agent", {"body": "take the teacher cache"})

    assert utterance is not None
    assert (utterance.kind, utterance.audience, utterance.text) == (
        "message",
        "distill-agent",
        "take the teacher cache",
    )


def test_a_review_reads_as_its_reason() -> None:
    utterance = extract_event_utterance(
        SAID,
        "review",
        None,
        {"number": 4, "approved": False, "reason": "blocks: cuts coverage"},
    )

    assert utterance is not None
    assert (utterance.audience, utterance.text) == ("PR #4", "blocks: cuts coverage")
