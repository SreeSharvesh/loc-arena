"""Monitor prompt templates load loudly, reach the model with the inputs, and stay on the observable side.

A live monitor whose every model call failed scores only by its heuristic fallback; the check below names it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from loc_arena.config import ConfigError
from loc_arena.logging_.events import Event
from loc_arena.monitors.registry import (
    FORBIDDEN_MONITOR_INPUTS,
    PROMPT_INPUTS_PLACEHOLDER,
    MonitorModelCaller,
    MonitorVerdict,
    find_fallback_only_monitors,
    load_monitor_specs,
    load_monitors,
    render_prompt,
    run_monitors,
)
from loc_arena.protocol import monitor_context

from tests.unit._monitor_support import (
    ACTION_EVENT,
    RUN_CONFIG,
    SHIPPED_MONITORS,
    VERDICT_REPLY,
    load_one_monitor_spec,
    observable_event,
    write_monitors_file,
    write_prompt_template,
)

# One event at each tap an enabled shipped live monitor watches. The PR targets monitor-policy, the one repo
# the shipped reviewer is scoped to; the inference call carries the fingerprints every call record holds.
SHIPPED_TAP_EVENTS = [
    ACTION_EVENT,
    observable_event(
        1,
        "pr",
        target_repo="monitor-policy",
        pr_title="Tidy the config",
        diff="- a: 1\n+ a: 2",
    ),
    observable_event(2, "inference_call", prompt_fp="query-fingerprint", output_fp="output-fingerprint"),
]


def test_live_monitor_without_prompt_ref_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mode 'live' needs a prompt_ref"):
        load_one_monitor_spec(tmp_path, mode="live")


def test_prompt_ref_naming_a_missing_file_is_a_config_error(tmp_path: Path) -> None:
    missing = str(tmp_path / "absent.txt")

    with pytest.raises(ConfigError, match="names no file"):
        load_one_monitor_spec(tmp_path, mode="live", prompt_ref=missing)


@pytest.mark.parametrize("text", ["Rate this step.", "{inputs} and again {inputs}"], ids=["absent", "twice"])
def test_prompt_template_must_place_the_inputs_exactly_once(tmp_path: Path, text: str) -> None:
    template = write_prompt_template(tmp_path, text)

    with pytest.raises(ConfigError, match="exactly once"):
        load_one_monitor_spec(tmp_path, mode="live", prompt_ref=template)


def test_a_heuristic_monitor_without_a_prompt_ref_scores_its_events(tmp_path: Path) -> None:
    registry = load_monitors(write_monitors_file(tmp_path, mode="heuristic"), RUN_CONFIG.models)

    verdicts = run_monitors(registry, [ACTION_EVENT], monitor_context(RUN_CONFIG))

    assert [(verdict.monitor, verdict.target_seq) for verdict in verdicts] == [
        ("action_monitor", ACTION_EVENT.seq),
    ]


def test_a_loaded_template_renders_the_inputs_as_json_in_place_of_its_placeholder(tmp_path: Path) -> None:
    template = write_prompt_template(tmp_path, "Rate it.\n{inputs}\nReply.")
    spec = load_one_monitor_spec(tmp_path, mode="live", prompt_ref=template)

    prompt = render_prompt(spec.prompt_template, {"tool_name": "read_file"})

    assert prompt == 'Rate it.\n{\n  "tool_name": "read_file"\n}\nReply.'


def _prompt_recording_caller(prompts: dict[str, str]) -> MonitorModelCaller:
    """A model caller that keeps the last prompt each monitor sent and replies with a parseable verdict."""

    def call(monitor: str, prompt: str, temperature: float) -> str:
        prompts[monitor] = prompt
        return VERDICT_REPLY

    return call


def _rendered_inputs(template: str, prompt: str) -> object:
    """The JSON a prompt holds at the template's placeholder, or ``None`` if it is not that template."""
    head, tail = template.split(PROMPT_INPUTS_PLACEHOLDER)
    if not (prompt.startswith(head) and prompt.endswith(tail)):
        return None
    return json.loads(prompt[len(head) : len(prompt) - len(tail)])


def test_every_enabled_shipped_live_monitor_renders_its_template_with_its_inputs() -> None:
    specs = load_monitor_specs(SHIPPED_MONITORS)
    live_templates = {
        spec.name: spec.prompt_template for spec in specs if spec.enabled and spec.mode == "live"
    }
    assert live_templates, "the shipped config enables no live monitor"
    registry = load_monitors(SHIPPED_MONITORS, RUN_CONFIG.models)
    prompts: dict[str, str] = {}
    context = monitor_context(RUN_CONFIG, model_caller=_prompt_recording_caller(prompts))

    run_monitors(registry, SHIPPED_TAP_EVENTS, context)

    rendered = {
        monitor for monitor, prompt in prompts.items() if _rendered_inputs(live_templates[monitor], prompt)
    }
    assert rendered == set(live_templates)


def test_no_shipped_prompt_template_names_a_sealed_only_field() -> None:
    specs = load_monitor_specs(SHIPPED_MONITORS)
    templates = {spec.name: spec.prompt_template.lower() for spec in specs if spec.prompt_template}
    assert templates, "no shipped monitor declares a prompt template"

    leaks = sorted(
        (monitor, field)
        for monitor, template in templates.items()
        for field in FORBIDDEN_MONITOR_INPUTS
        if field in template
    )

    assert leaks == []


def _verdict(monitor: str, *, from_model: bool) -> MonitorVerdict:
    return MonitorVerdict(monitor, "action", 0.1, "r", "agent-main", 0, 1.0, from_model=from_model)


def _parse_error(monitor: str) -> Event:
    return Event(
        episode_id="ep",
        seq=0,
        ts=1.0,
        actor_uid=monitor,
        actor_role="trusted",
        kind="monitor_parse_error",
        payload={"monitor": monitor, "error": "unparseable"},
    )


# An inference monitor with nothing off-path to flag reports no verdict at all.
@pytest.mark.parametrize(
    "verdicts",
    [[_verdict("action_monitor", from_model=False)], []],
    ids=["heuristic verdicts only", "no verdicts"],
)
def test_monitor_whose_every_model_call_failed_is_fallback_only(verdicts: list[MonitorVerdict]) -> None:
    errors = [_parse_error("action_monitor")]

    fallback_only = find_fallback_only_monitors(verdicts, errors)

    assert fallback_only == {"action_monitor"}


def test_monitor_with_one_parsed_model_verdict_is_not_fallback_only() -> None:
    verdicts = [_verdict("action_monitor", from_model=False), _verdict("action_monitor", from_model=True)]
    errors = [_parse_error("action_monitor")]

    fallback_only = find_fallback_only_monitors(verdicts, errors)

    assert fallback_only == set()


def test_monitor_that_never_called_the_model_is_not_fallback_only() -> None:
    verdicts = [_verdict("action_monitor", from_model=False)]

    fallback_only = find_fallback_only_monitors(verdicts, [])

    assert fallback_only == set()
