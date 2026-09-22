"""A compact, deterministic job-spec DSL.

A spec string names a job and its resource ask as whitespace-separated fields, for example::

    job eval cpu=2 mem=512 prio=high needs=prep,tokenize

The leading ``job`` keyword and a name are required; ``cpu``, ``mem``, ``prio``, ``needs``, and ``cmd`` are
optional. ``prio`` accepts a word (``low``/``normal``/``high``) or an integer; ``needs`` is a comma-separated
dependency list; ``cmd`` overrides the default command (a colon-joined argument vector). Any malformed field,
unknown key, or missing name raises :class:`SpecSyntaxError`. Parsing is pure and deterministic, so the same
string always yields the same :class:`~meridian_common.jobclient.models.JobSpec`.
"""

from __future__ import annotations

from meridian_common.jobclient.models import JobSpec
from meridian_jobsvc.errors import SpecSyntaxError

_PRIORITY_WORDS = {"low": 0, "normal": 10, "high": 20}
_KNOWN_KEYS = {"cpu", "mem", "prio", "needs", "cmd"}


def _parse_cpu(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise SpecSyntaxError(f"cpu must be a number, got {raw!r}", path="cpu") from exc
    if value <= 0:
        raise SpecSyntaxError(f"cpu must be positive, got {value}", path="cpu")
    return value


def _parse_mem(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise SpecSyntaxError(f"mem must be an integer, got {raw!r}", path="mem") from exc
    if value <= 0:
        raise SpecSyntaxError(f"mem must be positive, got {value}", path="mem")
    return value


def _parse_priority(raw: str) -> int:
    if raw in _PRIORITY_WORDS:
        return _PRIORITY_WORDS[raw]
    try:
        return int(raw)
    except ValueError as exc:
        raise SpecSyntaxError(
            f"prio must be one of {sorted(_PRIORITY_WORDS)} or an integer, got {raw!r}", path="prio"
        ) from exc


def _parse_needs(raw: str) -> tuple[str, ...]:
    deps = tuple(part for part in raw.split(",") if part)
    if not deps:
        raise SpecSyntaxError("needs must list at least one dependency", path="needs")
    return deps


def _parse_cmd(raw: str) -> list[str]:
    argv = [part for part in raw.split(":") if part]
    if not argv:
        raise SpecSyntaxError("cmd must have at least one argument", path="cmd")
    return argv


def parse_spec(text: str) -> JobSpec:
    """Parse a job-spec DSL string into a :class:`JobSpec`.

    Raises:
        SpecSyntaxError: if the string is empty, lacks the ``job <name>`` head, repeats or misuses a key,
            names an unknown key, or gives a malformed value.
    """
    tokens = text.split()
    if len(tokens) < 2 or tokens[0] != "job":
        raise SpecSyntaxError(f"a spec must start with 'job <name>', got {text!r}", path="job")
    name = tokens[1]
    if "=" in name:
        raise SpecSyntaxError(f"missing job name in {text!r}", path="name")

    cpu = 1.0
    memory_mb = 512
    priority = 0
    depends_on: tuple[str, ...] = ()
    command: list[str] | None = None
    seen: set[str] = set()

    for token in tokens[2:]:
        if "=" not in token:
            raise SpecSyntaxError(f"expected key=value, got {token!r}", path=token)
        key, _, value = token.partition("=")
        if key not in _KNOWN_KEYS:
            raise SpecSyntaxError(f"unknown field {key!r}", path=key)
        if key in seen:
            raise SpecSyntaxError(f"duplicate field {key!r}", path=key)
        seen.add(key)
        if value == "":
            raise SpecSyntaxError(f"field {key!r} has no value", path=key)
        if key == "cpu":
            cpu = _parse_cpu(value)
        elif key == "mem":
            memory_mb = _parse_mem(value)
        elif key == "prio":
            priority = _parse_priority(value)
        elif key == "needs":
            depends_on = _parse_needs(value)
        elif key == "cmd":
            command = _parse_cmd(value)

    return JobSpec(
        name=name,
        command=command if command is not None else ["run", name],
        cpu=cpu,
        memory_mb=memory_mb,
        priority=priority,
        depends_on=depends_on,
    )
