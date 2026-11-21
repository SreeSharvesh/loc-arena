"""Read a run's prompts from their Markdown files, and resolve a run config's references to them.

Every prompt is a Markdown file. The engine's own, true of any scenario, are in ``loc_arena/prompts/``; a
scenario's are in ``scenarios/<name>/prompts/``. A run config's ``prompts`` block names each scenario file it
sends: an entry is a file's path, relative to that folder, or a list of further ``prompts.*`` references,
which composes one text from those fragments, joined by blank lines. A config variant overrides one entry,
and deep-merge keeps the rest. A prompt is set only by its ``*_ref`` key. See docs/run-config.md.

A file's text is its content less its final line break. A ``$name`` in it is a placeholder
(``string.Template``): an engine file names exactly the values the engine fills, and a scenario file takes
none, so ``$$`` there is a dollar sign. A missing file, a path outside the folder, a placeholder with no
value, inline text, a reference to a missing entry, a non-string fragment or a cycle fails loud with a
``ValueError`` naming where, which the config loader turns into a ``ConfigError``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from string import Template
from typing import Final

from scenarios.loader import SCENARIOS_ROOT

ENGINE_PROMPTS_DIRECTORY: Final = Path(__file__).parent / "prompts"
PROMPTS_KEY: Final = "prompts"
PROMPT_REFERENCE_PREFIX: Final = f"{PROMPTS_KEY}."
REFERENCE_SUFFIX: Final = "_ref"
PROMPT_SUFFIX: Final = ".md"
PROMPT_ENCODING: Final = "utf-8"
RUN_PROMPTS: Final = ("workspace_prompt",)
COVERT_PROMPTS: Final = ("objective_prompt",)
AGENT_PROMPTS: Final = ("system_prompt", "brief", "covert_brief")
REMOVED_PROMPTS_KEY: Final = "prompts_inline"


def read_engine_prompts(directory: Path, placeholders: Mapping[str, frozenset[str]]) -> dict[str, Template]:
    """The prompt files of ``directory``, by name, each checked to name exactly ``placeholders[name]``.

    Raises ``ValueError`` on a file that ``placeholders`` does not declare, which nothing would read, and on a
    file whose placeholders differ from its declared ones.
    """
    if unread := sorted({path.stem for path in directory.glob(f"*{PROMPT_SUFFIX}")} - set(placeholders)):
        raise ValueError(f"no code reads these prompt files of {directory}: {', '.join(unread)}")
    templates = {name: _read_template(directory / f"{name}{PROMPT_SUFFIX}") for name in placeholders}
    for name, template in templates.items():
        if set(template.get_identifiers()) != placeholders[name]:
            raise ValueError(
                f"{name}{PROMPT_SUFFIX} names {sorted(template.get_identifiers())}, but the engine fills "
                f"{sorted(placeholders[name])}",
            )
    return templates


def scenario_prompts(scenario: str) -> Path:
    """The folder of ``scenario``'s prompt files."""
    return SCENARIOS_ROOT / scenario / PROMPTS_KEY


def resolve_run_prompts(data: dict[str, object], root: Path | None) -> dict[str, object]:
    """``data`` with each ``*_ref`` key resolved to its text, and its ``prompts`` block to each entry's text.

    ``root`` is the folder of the run scenario's prompt files, which ``prompts`` entries name.
    """
    if REMOVED_PROMPTS_KEY in data:
        raise ValueError(f"{REMOVED_PROMPTS_KEY} is gone: name each prompt's file under {PROMPTS_KEY}")
    block = data.get(PROMPTS_KEY, {})
    if not isinstance(block, dict):
        raise ValueError(f"{PROMPTS_KEY} must map each name to a file or a list of references, got {block!r}")
    prompts = {
        name: _read_scenario_prompt(entry, root, f"{PROMPTS_KEY}.{name}") if isinstance(entry, str) else entry
        for name, entry in block.items()
    }
    resolved = {
        where: {
            key: "\n\n".join(_walk(value, prompts, f"{where}.{key}")) for key, value in references.items()
        }
        for where, references in _references(data).items()
    }
    run = data | resolved["run"] | {PROMPTS_KEY: prompts}
    if isinstance(covert := data.get("covert"), dict):
        run["covert"] = covert | resolved["covert"]
    if isinstance(agents := data.get("agents"), list):
        run["agents"] = [
            agent | resolved[f"agents.{index}"] if isinstance(agent, dict) else agent
            for index, agent in enumerate(agents)
        ]
    return run


def find_unread_prompt_files(documents: Mapping[str, Mapping[str, object]], scenario: str) -> list[str]:
    """The files of ``scenario``'s prompts folder that no reference of its ``documents``, by name, reaches.

    Raises ``ValueError`` naming a document whose references do not resolve.
    """
    root = scenario_prompts(scenario).resolve()
    read: set[Path] = set()
    for name, document in documents.items():
        if document.get("scenario") != scenario or not isinstance(block := document.get(PROMPTS_KEY), dict):
            continue
        try:
            read |= {
                (root / entry).resolve()
                for where, references in _references(document).items()
                for key, reference in references.items()
                for entry in _walk(reference, block, f"{where}.{key}")
            }
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from exc
    return [
        str(path.relative_to(root)) for path in sorted(root.rglob(f"*{PROMPT_SUFFIX}")) if path not in read
    ]


def _references(data: Mapping[str, object]) -> dict[str, dict[str, object]]:
    """Each prompt block's ``*_ref`` keys and their references, by where the block is.

    Raises ``ValueError`` on a prompt written inline under its field's own name.
    """
    agents = data.get("agents")
    blocks: list[tuple[str, object, tuple[str, ...]]] = [
        ("run", data, RUN_PROMPTS),
        ("covert", data.get("covert"), COVERT_PROMPTS),
    ]
    if isinstance(agents, list):
        blocks += [(f"agents.{index}", agent, AGENT_PROMPTS) for index, agent in enumerate(agents)]
    found: dict[str, dict[str, object]] = {}
    for where, block, fields in blocks:
        if not isinstance(block, Mapping):
            continue
        if inline := sorted(set(fields) & set(block)):
            raise ValueError(
                f"{where}: {', '.join(inline)} written inline; name a file with its {REFERENCE_SUFFIX} key",
            )
        keys = [f"{field}{REFERENCE_SUFFIX}" for field in fields]
        found[where] = {key: block[key] for key in keys if key in block}
    return found


def _walk(reference: object, prompts: object, where: str) -> list[str]:
    """The entries one reference composes, in order.

    Resolution is an explicit depth-first walk, not recursion, so even a pathological chain of references
    fails loud on a cycle or a bad fragment rather than raising ``RecursionError``. Each pending reference
    carries the set of names active on its own branch, so a cycle is caught while distinct siblings are not.
    """
    fragments: list[str] = []
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
            fragments.append(entry)
        elif isinstance(entry, list):
            pending.extend((part, active | {name}) for part in reversed(entry))
        else:
            raise ValueError(f"{where}: {PROMPTS_KEY} entry {name!r} is not a file or a list of references")
    return fragments


def _read_scenario_prompt(relative: str, root: Path | None, where: str) -> str:
    """The text of the scenario prompt file at ``relative``, which takes no placeholder."""
    if root is None:
        raise ValueError(f"{where}: a prompt file needs the run's scenario, and this run names none")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or path.suffix != PROMPT_SUFFIX or not path.is_file():
        raise ValueError(f"{where}: {relative!r} names no {PROMPT_SUFFIX} file in {root}")
    try:
        return _read_template(path).substitute()
    except KeyError as missing:
        raise ValueError(
            f"{where}: {relative} has the placeholder ${missing.args[0]}, which nothing fills",
        ) from None


def _read_template(path: Path) -> Template:
    template = Template(path.read_text(encoding=PROMPT_ENCODING).removesuffix("\n"))
    if not template.is_valid():
        raise ValueError(f"{path}: a $ that starts no placeholder; write $$ for a dollar sign")
    return template


# The engine's prompt files, by name, with the values each one names; read and checked on import.
ENGINE_PROMPTS: Final = read_engine_prompts(
    ENGINE_PROMPTS_DIRECTORY,
    {
        "identity": frozenset({"agent"}),
        "manager": frozenset({"manager"}),
        "reports": frozenset({"reports"}),
        "skills": frozenset({"directory"}),  # where the skills are (https://agentskills.io/specification)
        "services": frozenset({"services"}),  # the sandbox image has no curl
        "service": frozenset({"host", "port", "description"}),
        "service-access": frozenset({"identity", "rights"}),
        "telemetry": frozenset({"stores"}),
        "covert-steps": frozenset(),  # every covert brief ends with it
        "begin": frozenset(),  # the first prompt of every agent's session
    },
)
