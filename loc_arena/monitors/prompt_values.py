"""A monitor's prompt: the engine's template, the scenario's facts filled, ``$inputs`` left for each event.

A monitor prompt in ``loc_arena/prompts/`` is true of any scenario: each fact of one scenario in it, such as
the company's name or its repositories, is a ``$placeholder``. ``monitors.yaml``'s ``prompt_values`` maps each
one to the scenario prompt file that holds its value, and the loader fills them once. ``$inputs`` stays a
placeholder, which each event's declared inputs fill when the prompt is rendered. See
docs/run-config.md#prompts.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from re import Match
from string import Template
from typing import Final

from scenarios.loader import read_prompt

from loc_arena.config import ConfigError

PROMPT_INPUTS: Final = "inputs"  # the placeholder, $inputs, where a monitor prompt takes its declared inputs
PROMPT_INPUTS_PLACEHOLDER: Final = f"${PROMPT_INPUTS}"


def read_prompt_values(declared: object) -> dict[str, tuple[str, str]]:
    """Each placeholder's value and the file it came from, from ``monitors.yaml``'s ``prompt_values``.

    Raises ``ConfigError`` on a mapping that is not placeholder to file, or on a file that cannot be read.
    """
    if declared is None:
        return {}
    if not isinstance(declared, dict) or not all(isinstance(path, str) for path in declared.values()):
        raise ConfigError(f"prompt_values must map each placeholder to its value's file, got {declared!r}")
    values: dict[str, tuple[str, str]] = {}
    for name, source in declared.items():
        try:
            values[str(name)] = (read_prompt(Path(source)).substitute(), source)
        except (OSError, KeyError, ValueError) as exc:
            raise ConfigError(f"prompt_values.{name}: {source} is no prompt file of a value: {exc}") from exc
    return values


def load_prompt_template(
    name: str,
    prompt_ref: str | None,
    mode: str,
    values: Mapping[str, tuple[str, str]],
) -> tuple[str, tuple[str, ...]]:
    """Monitor ``name``'s prompt template, its facts filled from ``values``; and the files it was made of.

    A ``prompt_ref`` must name a prompt file that places ``$inputs`` exactly once, and a live monitor must
    declare one: without it the model gets raw JSON and no instruction to answer. Every other placeholder must
    have a value. Raises ``ConfigError`` naming what is wrong.
    """
    if prompt_ref is None:
        if mode == "live":
            raise ConfigError(f"monitor {name!r}: mode 'live' needs a prompt_ref (its prompt template)")
        return "", ()
    path = Path(prompt_ref)
    if not path.is_file():
        raise ConfigError(f"monitor {name!r}: prompt_ref {prompt_ref!r} names no file ({path.resolve()})")
    try:
        template = read_prompt(path)
    except ValueError as exc:
        raise ConfigError(f"monitor {name!r}: {exc}") from exc
    placed = [_placed(match) for match in template.pattern.finditer(template.template)]
    if placed.count(PROMPT_INPUTS) != 1:
        raise ConfigError(f"monitor {name!r}: {prompt_ref} must place ${PROMPT_INPUTS} exactly once")
    facts = [fact for fact in dict.fromkeys(placed) if fact and fact != PROMPT_INPUTS]
    if missing := [fact for fact in facts if fact not in values]:
        raise ConfigError(
            f"monitor {name!r}: {prompt_ref} names ${', $'.join(missing)}, with no prompt_values",
        )
    return _fill(template, values), (prompt_ref, *(values[fact][1] for fact in facts))


def _placed(match: Match[str]) -> str:
    """The placeholder ``match`` names, or nothing for an escaped dollar sign."""
    return match.group("named") or match.group("braced") or ""


def _fill(template: Template, values: Mapping[str, tuple[str, str]]) -> str:
    """``template`` with each fact's value in place, ``$inputs`` and each ``$$`` kept for rendering."""

    def replace(match: Match[str]) -> str:
        fact = _placed(match)
        if not fact or fact == PROMPT_INPUTS:
            return match.group(0)
        return values[fact][0].replace("$", "$$")

    return template.pattern.sub(replace, template.template)
