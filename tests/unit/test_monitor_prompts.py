"""Monitor prompt templates load loudly and stay on the observable side; fallback-only monitors show."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from loc_arena.config import ConfigError
from loc_arena.logging_.events import Event
from loc_arena.monitors.registry import (
    FORBIDDEN_MONITOR_INPUTS,
    PROMPT_INPUTS_PLACEHOLDER,
    MonitorSpec,
    MonitorVerdict,
    find_fallback_only_monitors,
    load_monitor_specs,
    render_prompt,
)


def _load_one(tmp_path: Path, **entry: object) -> MonitorSpec:
    """Load a monitors.yaml holding one monitor entry through the loader the config check uses."""
    monitor = {"name": "m", "tap": ["action"], "inputs": ["tool_name"], "model": "trusted_monitor", **entry}
    path = tmp_path / "monitors.yaml"
    path.write_text(yaml.safe_dump({"monitors": [monitor]}))
    (spec,) = load_monitor_specs(path)
    return spec


def _template(tmp_path: Path, text: str) -> str:
    path = tmp_path / "template.txt"
    path.write_text(text)
    return str(path)


def test_live_monitor_without_prompt_ref_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mode 'live' needs a prompt_ref"):
        _load_one(tmp_path, mode="live")


def test_prompt_ref_naming_a_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="names no file"):
        _load_one(tmp_path, mode="live", prompt_ref=str(tmp_path / "absent.txt"))


@pytest.mark.parametrize("text", ["Rate this step.", "{inputs} and again {inputs}"])
def test_prompt_template_must_place_the_inputs_exactly_once(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match="exactly once"):
        _load_one(tmp_path, mode="live", prompt_ref=_template(tmp_path, text))


def test_heuristic_monitor_without_prompt_ref_loads_with_no_template(tmp_path: Path) -> None:
    spec = _load_one(tmp_path, mode="heuristic")
    assert spec.prompt_ref is None
    assert spec.prompt_template == ""


def test_prompt_template_is_loaded_once_and_rendered_with_the_inputs(tmp_path: Path) -> None:
    spec = _load_one(tmp_path, mode="live", prompt_ref=_template(tmp_path, "Rate it.\n{inputs}\nReply."))
    prompt = render_prompt(spec.prompt_template, {"tool_name": "read_file"})
    assert prompt == 'Rate it.\n{\n  "tool_name": "read_file"\n}\nReply.'


def test_every_shipped_model_monitor_has_a_template_free_of_sealed_field_names() -> None:
    model_monitors = [s for s in load_monitor_specs("configs/monitors.yaml") if s.model != "none"]
    assert {s.name for s in model_monitors} == {
        "action_monitor",
        "inference_monitor",
        "pr_review_monitor",
        "shared_surface_monitor",
    }
    for spec in model_monitors:
        assert PROMPT_INPUTS_PLACEHOLDER in spec.prompt_template, spec.name
        leaked = {name for name in FORBIDDEN_MONITOR_INPUTS if name in spec.prompt_template.lower()}
        assert not leaked, f"{spec.name} prompt names sealed-only fields {sorted(leaked)}"


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


def test_monitor_whose_every_model_call_failed_is_fallback_only() -> None:
    # heuristic verdicts only, or none at all (an inference monitor with nothing off-path to flag)
    verdicts = [_verdict("action_monitor", from_model=False)]
    errors = [_parse_error("action_monitor"), _parse_error("inference_monitor")]
    assert find_fallback_only_monitors(verdicts, errors) == {"action_monitor", "inference_monitor"}


def test_monitor_with_one_parsed_model_verdict_is_not_fallback_only() -> None:
    verdicts = [_verdict("action_monitor", from_model=False), _verdict("action_monitor", from_model=True)]
    assert find_fallback_only_monitors(verdicts, [_parse_error("action_monitor")]) == set()


def test_monitor_that_never_called_the_model_is_not_fallback_only() -> None:
    assert find_fallback_only_monitors([_verdict("action_monitor", from_model=False)], []) == set()
