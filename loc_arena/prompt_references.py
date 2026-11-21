"""Read a run's prompts from their Markdown files, and resolve a run config's references to them.

Every prompt is a Markdown file named ``<whose>.<part>[.<variant>].md``. The engine's own, true of any
scenario, are in ``loc_arena/prompts/``; a scenario's are in ``scenarios/<name>/prompts/``. A run config's
``prompts`` block names each scenario file it sends, under a key equal to the file's name less ``.md``: an
entry is a file's path, relative to that folder, or a list of further ``prompts.*`` references, which composes
one text from those fragments, joined by blank lines. A variant config points one entry at
``<key>.<variant>.md``, and deep-merge keeps the rest. A prompt is set only by its ``*_ref`` key. See
docs/run-config.md#prompts.

A file's text is its content less its final line break. A ``$name`` in it is a placeholder
(``string.Template``): an engine file names exactly the values the engine fills, and a scenario file takes
none, so ``$$`` there is a dollar sign. A missing file, a path outside the folder, a file not named after
its key, a placeholder with no value, inline text, a reference to a missing entry, a non-string fragment or a
cycle fails loud with a ``ValueError`` naming where, which the config loader turns into a ``ConfigError``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from string import Template
from typing import Final

from pydantic.dataclasses import dataclass
from scenarios.loader import (
    PROMPTS_DIRECTORY,
    SCENARIOS_ROOT,
    SERVICE_PROMPT_SUFFIX,
    load_scenario,
    read_prompt,
)

REPOSITORY: Final = SCENARIOS_ROOT.parent
ENGINE_PROMPTS_DIRECTORY: Final = Path(__file__).parent / PROMPTS_DIRECTORY
PROMPTS_KEY: Final = "prompts"
PROMPT_REFERENCE_PREFIX: Final = f"{PROMPTS_KEY}."
REFERENCE_SUFFIX: Final = "_ref"
PROMPT_SUFFIX: Final = ".md"
SECTIONS_KEY: Final = "prompt_sections"
RUN_PROMPTS: Final = ("workspace_prompt", "task_description")
COVERT_PROMPTS: Final = ("objective_prompt",)
AGENT_PROMPTS: Final = ("system_prompt", "brief", "covert_brief")
REMOVED_PROMPTS_KEY: Final = "prompts_inline"


@dataclass(frozen=True)
class Section:
    """A part of a prompt as sent: its text, and the files it came from, from the repository's root."""

    sources: tuple[str, ...]
    text: str


Sections = tuple[Section, ...]


def read_engine_prompts(directory: Path, placeholders: Mapping[str, frozenset[str]]) -> dict[str, Template]:
    """The prompt files of ``directory``, by name, each checked to name exactly ``placeholders[name]``.

    Raises ``ValueError`` on a file that ``placeholders`` does not declare, which nothing would read, and on a
    file whose placeholders differ from its declared ones.
    """
    if unread := sorted({path.stem for path in directory.glob(f"*{PROMPT_SUFFIX}")} - set(placeholders)):
        raise ValueError(f"no code reads these prompt files of {directory}: {', '.join(unread)}")
    templates = {name: read_prompt(directory / f"{name}{PROMPT_SUFFIX}") for name in placeholders}
    for name, template in templates.items():
        if set(template.get_identifiers()) != placeholders[name]:
            raise ValueError(
                f"{name}{PROMPT_SUFFIX} names {sorted(template.get_identifiers())}, but the engine fills "
                f"{sorted(placeholders[name])}",
            )
    return templates


def engine_source(name: str) -> str:
    """Where the engine's prompt file ``name`` is, relative to the repository."""
    return str((ENGINE_PROMPTS_DIRECTORY / f"{name}{PROMPT_SUFFIX}").relative_to(REPOSITORY))


def scenario_prompts(scenario: str) -> Path:
    """The folder of ``scenario``'s prompt files."""
    return SCENARIOS_ROOT / scenario / PROMPTS_DIRECTORY


def resolve_run_prompts(data: dict[str, object], root: Path | None) -> dict[str, object]:
    """``data`` with each ``*_ref`` key resolved to its text, and its ``prompts`` block to each entry's text.

    ``root`` is the folder of the run scenario's prompt files, which ``prompts`` entries name. The result
    also holds, under ``prompt_sections``, each resolved prompt's sections, by ``<block>.<field>``, where an
    agent's block is its id.
    """
    _reject_removed_keys(data)
    block = data.get(PROMPTS_KEY, {})
    if not isinstance(block, dict):
        raise ValueError(f"{PROMPTS_KEY} must map each name to a file or a list of references, got {block!r}")
    files = {
        name: _read_scenario_prompt(name, entry, root)
        for name, entry in block.items()
        if isinstance(entry, str)
    }
    resolved, sections = _resolve_references(data, block, files)
    texts = {name: files[name].text if name in files else entry for name, entry in block.items()}
    run = data | resolved.get("run", {}) | {PROMPTS_KEY: texts, SECTIONS_KEY: sections}
    if isinstance(covert := data.get("covert"), dict):
        run["covert"] = covert | resolved.get("covert", {})
    if isinstance(agents := data.get("agents"), list):
        run["agents"] = [
            agent | resolved.get(f"agents.{index}", {}) if isinstance(agent, dict) else agent
            for index, agent in enumerate(agents)
        ]
    return run


def _reject_removed_keys(data: Mapping[str, object]) -> None:
    """Raise ``ValueError`` on a key that held prompt text before the text moved to files."""
    if REMOVED_PROMPTS_KEY in data:
        raise ValueError(f"{REMOVED_PROMPTS_KEY} is gone: name each prompt's file under {PROMPTS_KEY}")
    if isinstance(main_task := data.get("main_task"), dict) and "goal" in main_task:
        raise ValueError(
            "main_task.goal is gone: name the monitors' task description's file in task_description_ref",
        )


def _resolve_references(
    data: Mapping[str, object],
    block: Mapping[str, object],
    files: Mapping[str, Section],
) -> tuple[dict[str, dict[str, str]], dict[str, list[Section]]]:
    """Each ``*_ref`` key's text, by where its block is; and its sections, by ``<owner>.<field>``."""
    sections: dict[str, list[Section]] = {}
    resolved: dict[str, dict[str, str]] = {}
    for where, (owner, references) in _references(data).items():
        for key, reference in references.items():
            parts = [files[name] for name in _walk(reference, block, f"{where}.{key}")]
            sections[f"{owner}.{key.removesuffix(REFERENCE_SUFFIX)}"] = parts
            resolved.setdefault(where, {})[key] = "\n\n".join(part.text for part in parts)
    return resolved, sections


def find_unread_prompt_files(documents: Mapping[str, Mapping[str, object]], scenario: str) -> list[str]:
    """The files of ``scenario``'s prompts folder that no reference of its ``documents``, by name, reaches.

    A service's description, which the scenario's loader reads, counts as read. Raises ``ValueError`` naming
    a document whose references do not resolve.
    """
    root = scenario_prompts(scenario).resolve()
    read: set[Path] = set()
    for name, document in documents.items():
        if document.get("scenario") != scenario or not isinstance(block := document.get(PROMPTS_KEY), dict):
            continue
        try:
            read |= {
                (root / str(block[entry])).resolve()
                for where, (_, references) in _references(document).items()
                for key, reference in references.items()
                for entry in _walk(reference, block, f"{where}.{key}")
            }
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from exc
    return [
        str(path.relative_to(root))
        for path in sorted(root.rglob(f"*{PROMPT_SUFFIX}"))
        if path not in read and path not in _told_service_descriptions(scenario)
    ]


def _told_service_descriptions(scenario: str) -> set[Path]:
    """``scenario``'s ``<service>.service.md`` files a brief can tell: a reached service's, a store's."""
    services = load_scenario(scenario).live_services
    told = [service for service in services if (not service.tools or service.rights) or service.collects]
    root = scenario_prompts(scenario)
    return {(root / f"{service.name}{SERVICE_PROMPT_SUFFIX}").resolve() for service in told}


def _references(data: Mapping[str, object]) -> dict[str, tuple[str, dict[str, object]]]:
    """Each prompt block's owner and its ``*_ref`` keys with their references, by where the block is.

    Raises ``ValueError`` on a prompt written inline under its field's own name.
    """
    agents = data.get("agents")
    blocks: list[tuple[str, str, object, tuple[str, ...]]] = [
        ("run", "run", data, RUN_PROMPTS),
        ("covert", "covert", data.get("covert"), COVERT_PROMPTS),
    ]
    if isinstance(agents, list):
        blocks += [
            (
                f"agents.{index}",
                str(agent.get("id", index)) if isinstance(agent, Mapping) else "",
                agent,
                AGENT_PROMPTS,
            )
            for index, agent in enumerate(agents)
        ]
    found: dict[str, tuple[str, dict[str, object]]] = {}
    for where, owner, block, fields in blocks:
        if not isinstance(block, Mapping):
            continue
        if inline := sorted(set(fields) & set(block)):
            raise ValueError(
                f"{where}: {', '.join(inline)} written inline; name a file with its {REFERENCE_SUFFIX} key",
            )
        keys = [f"{field}{REFERENCE_SUFFIX}" for field in fields]
        found[where] = (owner, {key: block[key] for key in keys if key in block})
    return found


def _walk(reference: object, prompts: object, where: str) -> list[str]:
    """The names of the file entries one reference composes, in order.

    Resolution is an explicit depth-first walk, not recursion, so even a pathological chain of references
    fails loud on a cycle or a bad fragment rather than raising ``RecursionError``. Each pending reference
    carries the set of names active on its own branch, so a cycle is caught while distinct siblings are not.
    """
    names: list[str] = []
    pending: list[tuple[object, frozenset[str]]] = [(reference, frozenset())]
    while pending:
        current, active = pending.pop()
        if not isinstance(current, str) or not current.startswith(PROMPT_REFERENCE_PREFIX):
            raise ValueError(
                f"{where}: only {PROMPT_REFERENCE_PREFIX}* references are resolved here, got {current!r}",
            )
        name = current.removeprefix(PROMPT_REFERENCE_PREFIX)
        if not isinstance(prompts, dict) or name not in prompts:
            raise ValueError(f"{where}: reference {current!r} not found in {PROMPTS_KEY}")
        if name in active:
            raise ValueError(f"{where}: {PROMPTS_KEY} reference {name!r} is part of a cycle")
        entry = prompts[name]
        if isinstance(entry, str):
            names.append(name)
        elif isinstance(entry, list):
            pending.extend((part, active | {name}) for part in reversed(entry))
        else:
            raise ValueError(f"{where}: {PROMPTS_KEY} entry {name!r} is not a file or a list of references")
    return names


def _read_scenario_prompt(name: str, relative: str, root: Path | None) -> Section:
    """Entry ``name``'s file at ``relative``, named ``<name>[.<variant>].md``, which takes no placeholder."""
    where = f"{PROMPTS_KEY}.{name}"
    if root is None:
        raise ValueError(f"{where}: a prompt file needs the run's scenario, and this run names none")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix != PROMPT_SUFFIX or not path.is_file():
        raise ValueError(f"{where}: {relative!r} names no {PROMPT_SUFFIX} file in {root}")
    variant = path.stem.removeprefix(f"{name}.") if path.stem.startswith(f"{name}.") else ""
    if name.count(".") != 1 or not (path.stem == name or (variant and "." not in variant)):
        raise ValueError(
            f"{where}: a file's key is <whose>.<part>, and {relative} is not named after it: name it "
            f"{name}.md or {name}.<variant>.md",
        )
    try:
        text = read_prompt(path).substitute()
    except KeyError as missing:
        raise ValueError(
            f"{where}: {relative} has the placeholder ${missing.args[0]}, which nothing fills",
        ) from None
    return Section((str(path.relative_to(REPOSITORY.resolve())),), text)


# The engine's prompt files, by name, with the values each one names; read and checked on import.
ENGINE_PROMPTS: Final = read_engine_prompts(
    ENGINE_PROMPTS_DIRECTORY,
    {
        "agent.identity": frozenset({"agent"}),
        "agent.manager": frozenset({"manager"}),
        "agent.reports": frozenset({"reports"}),
        "agent.skills": frozenset(
            {"directory"},
        ),  # where the skills are (https://agentskills.io/specification)
        "agent.services": frozenset({"services"}),  # the sandbox image has no curl
        "agent.service": frozenset({"host", "port", "description"}),
        "agent.service-access": frozenset({"identity", "rights"}),
        "agent.telemetry": frozenset({"stores"}),
        "agent.covert-steps": frozenset(),  # every covert brief ends with it
        "agent.first-turn": frozenset(),
        "agent.notice": frozenset({"text"}),  # a notice of a subscribed topic, given as a turn
        "agent.notice-dm": frozenset({"actor"}),
        "agent.notice-mention": frozenset({"actor", "channel"}),
        "agent.notice-thread-mention": frozenset({"actor"}),
        "agent.notice-channel": frozenset({"actor", "channel"}),
        "agent.notice-job": frozenset({"name", "job", "state"}),
        "agent.notice-pull-request": frozenset({"actor", "action", "number", "repo"}),
        "agent.notice-ticket": frozenset({"actor"}),
        "iam.service": frozenset(),
        "coworker.batch": frozenset(),  # the scripted coworker's one model call, its noise on the gateway
        "action.monitor": frozenset({"inputs"}),
        "inference.monitor": frozenset({"inputs"}),
        "pr-review.monitor": frozenset({"inputs"}),
        "reader.system": frozenset({"claim_limit"}),
        "reader.brief.agents": frozenset(),
        "reader.brief.monitors": frozenset(),
        "reader.brief.ask": frozenset(),
        "reader.first-turn": frozenset(),
        "reader.first-turn.ask": frozenset({"question"}),
    },
)
