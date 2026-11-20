"""Resolve a run config's ``prompts_inline.*`` references.

A prompt is set only by its ``*_ref`` key. A reference resolves to its ``prompts_inline`` entry; an entry that
is a list of further references composes one brief from those fragments, joined by blank lines, so a generic
core is written once and a killchain config specializes a brief by overriding one fragment. A reference to a
missing entry, a non-string fragment, or a cycle fails loud with a ``ValueError`` naming the path, which the
config loader turns into a ``ConfigError``. See docs/run-config.md.
"""

from __future__ import annotations

from typing import Final

PROMPT_REFERENCE_PREFIX: Final = "prompts_inline."
REFERENCE_SUFFIX: Final = "_ref"


def resolve_block_prompts(
    block: object,
    prompt_fields: tuple[str, ...],
    inline_prompts: object,
    where: str,
) -> object:
    """Resolve the block's ``<field>_ref`` keys to their prompt text, leaving its other keys as written."""
    if not isinstance(block, dict):
        return block
    references = {f"{field_name}{REFERENCE_SUFFIX}" for field_name in prompt_fields}
    kept = {key: value for key, value in block.items() if key not in prompt_fields}
    return kept | {
        key: resolve_prompt_reference(value, inline_prompts, f"{where}.{key}")
        for key, value in kept.items()
        if key in references
    }


def resolve_prompt_reference(reference: object, inline_prompts: object, where: str) -> str:
    """One ``prompts_inline.*`` reference's text; a list entry composes its fragment references, joined.

    Resolution is an explicit depth-first walk, not recursion, so even a pathological chain of references
    fails loud on a cycle or a bad fragment rather than raising ``RecursionError``. Each pending reference
    carries the set of names active on its own branch, so a cycle is caught while distinct siblings are not.
    """
    fragments: list[str] = []
    pending: list[tuple[object, frozenset[str]]] = [(reference, frozenset())]
    while pending:
        current, active = pending.pop()
        if not isinstance(current, str) or not current.startswith(PROMPT_REFERENCE_PREFIX):
            raise ValueError(f"{where}: only prompts_inline.* references are resolved here, got {current!r}")
        name = current.removeprefix(PROMPT_REFERENCE_PREFIX)
        if not isinstance(inline_prompts, dict) or name not in inline_prompts:
            raise ValueError(f"{where}: reference {current!r} not found in prompts_inline")
        if name in active:
            raise ValueError(f"{where}: prompts_inline reference {name!r} is part of a cycle")
        entry = inline_prompts[name]
        if isinstance(entry, str):
            fragments.append(entry)
        elif isinstance(entry, list):
            pending.extend((part, active | {name}) for part in reversed(entry))
        else:
            raise ValueError(f"{where}: prompts_inline entry {name!r} is not a string or list of references")
    return "\n\n".join(fragments)
