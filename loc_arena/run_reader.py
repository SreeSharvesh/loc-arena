"""The run reader: a pi agent that reads one finished run and reports what its agents did.

A run's records are text that untrusted agents wrote, and the reader has a shell. So it is isolated as an
agent's loop is: it calls its model through the model gateway with a token of its own, and its container is
on the network with no route out. It mounts the run directory read-only, and one folder for its report. The
provider key stays in the gateway's container, and the gateway records the reader's calls.

The report (``readings/<brief>.md`` in the run directory) opens with facts this module counts from the
records (``summarize_run``), so its numbers do not depend on the reader; the reader's account follows. The
briefs are in ``reader_briefs/``: each says where every file is and how the logs join.
"""

from __future__ import annotations

import functools
import json
import os
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from pydantic import JsonValue, SecretStr

from loc_arena.compose_document import ComposeDocument, ComposeService
from loc_arena.config import RunConfig
from loc_arena.episode_stack import (
    AGENT_NETWORK,
    EGRESS_NETWORK,
    GATEWAY_MODE_OPTION,
    REPOSITORY,
    TOKEN_BYTES,
)
from loc_arena.explorer.scores import load_scores
from loc_arena.gateway import model_gateway
from loc_arena.gateway.loop_routes import LOOP_ROUTE_PREFIX, Loops
from loc_arena.logging_.events import read_events
from loc_arena.merged_main import EPISODE_PHASE, TWIN_PHASE
from loc_arena.monitors.registry import MonitorVerdict
from loc_arena.own_sessions import PI_ENVIRONMENT, PI_SETTINGS
from loc_arena.settings import LocArenaSettings
from loc_arena.yaml_documents import dump_document

READER = "reader"  # its compose service, its name as the gateway's caller, and its role in the models file
READER_TOKEN_SECRET_NAME = "reader_token"
READER_TOKEN_VARIABLE = "LOC_ARENA_READER_TOKEN"
READINGS = "readings"  # in the run directory: each brief's report, and the reader's calls beside it
BRIEFS = Path(__file__).parent / "reader_briefs"
_DATA, _HOME, _WORK = "/data", "/home/reader", "/work"  # in the reader's container
_REPORT = "report.md"
_PROVIDER = "gateway"
_ROUTE = f"{LOOP_ROUTE_PREFIX}/{READER}"  # the reader's own route at the gateway
_KICKOFF = "Read the run as your brief says, write the report file, then print it."


def _settle(config: RunConfig) -> LocArenaSettings:
    """A reading's settings: the run config's with one upstream, the one the reader calls."""
    gateway = config.settings.gateway.model_copy(update={"agents_upstream": None})
    return config.settings.model_copy(update={"gateway": gateway})


def issue_reader_secrets(config: RunConfig) -> dict[str, str]:
    """A reading's secrets by host variable: the reader's token, and the gateway config that admits it.

    The token opens one route, the reader's own, as an agent's loop has: it bounds the reader's calls at
    ``reader.model_calls`` and sends each upstream as the reader role's model, whatever the call names. The
    route goes to ``gateway.upstream``, whatever upstream a run gave its agents' loops, and refuses a call
    that names another model or a list of fallback models.
    """
    role, settings = config.models[READER], _settle(config)
    overrides: dict[str, JsonValue] = {
        "model": role.model,
        "temperature": role.temperature,
        "max_completion_tokens": role.max_tokens,
    }
    if (providers := config.model_providers.get(role.model)) is not None:
        overrides["provider"] = providers.model_dump(exclude_none=True)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    # One model, and no fallback list (OpenRouter's `models`), which OpenRouter could answer from instead.
    allow = f'json(request.body).model == "{role.model}" && !has(json(request.body).models)'
    calls = {READER: settings.reader.model_calls}
    loops = Loops(calls, overrides, caller=(READER, SecretStr(token)), allow=allow)
    # The record tags a reading's calls with the attack play: a gateway's record knows two plays.
    admitted = model_gateway.issue_gateway_secrets(
        settings,
        model_gateway.issue_held_tokens(TOKEN_BYTES),  # the episode's and the host's: nothing holds them
        {},
        "attack",
        badges=False,
        loops=loops,
    )
    return {READER_TOKEN_VARIABLE: token, **admitted}


def write_reader_home(home: Path, config: RunConfig, brief: str) -> list[str]:
    """Write the reader's home (pi's provider and settings, and ``brief``); return pi's command.

    Raises ``ValueError`` when no brief has that name.
    """
    names = sorted(path.stem for path in BRIEFS.glob("*.md"))
    if brief not in names:
        raise ValueError(f"no brief {brief!r}: the briefs are {', '.join(names)}")
    if READER not in config.models:
        raise ValueError(f"the run config's models file has no {READER!r} role")
    settings, role = config.settings, config.models[READER]
    directory = home / ".pi" / "agent"
    directory.mkdir(parents=True, exist_ok=True)
    provider = {
        "baseUrl": f"http://{model_gateway.MODEL_GATEWAY_SERVICE}:{settings.gateway.port}/{_ROUTE}/api/v1",
        "api": "openai-completions",
        "apiKey": f"!cat {settings.gateway.secrets_dir / READER_TOKEN_SECRET_NAME}",
        "models": [{"id": role.model, "maxTokens": role.max_tokens}],
    }
    (directory / "models.json").write_text(json.dumps({"providers": {_PROVIDER: provider}}))
    (directory / "settings.json").write_text(json.dumps(PI_SETTINGS))
    shutil.copy(BRIEFS / f"{brief}.md", home / "brief.md")
    return [
        *["pi", "--print", "--model", f"{_PROVIDER}/{role.model}", "--tools", "read,bash,write"],
        *["--no-mcp", "--no-extensions", "--no-context-files", "--no-skills", "--no-session"],
        *["--append-system-prompt", f"{_HOME}/brief.md", _KICKOFF],
    ]


def render_reader_compose(
    config: RunConfig,
    run_directory: Path,
    home: Path,
    work: Path,
    command: list[str],
) -> ComposeDocument:
    """The compose file of a reading: the gateway, the Collector, and the reader that runs ``command``."""
    settings = _settle(config)
    stack = settings.stack
    reader: ComposeService = {
        "image": stack.image,
        "entrypoint": command,
        "working_dir": _WORK,
        "user": f"{os.getuid()}:{os.getgid()}",  # so the report it writes is this user's
        "environment": {"HOME": _HOME, **PI_ENVIRONMENT},
        "networks": [AGENT_NETWORK],
        "secrets": [READER_TOKEN_SECRET_NAME],
        "volumes": [f"{run_directory}:{_DATA}:ro", f"{home}:{_HOME}", f"{work}:{_WORK}"],
        "cap_drop": ["ALL"],
        "mem_limit": stack.sandbox_memory_limit,
        "cpus": stack.sandbox_cpus,
        "pids_limit": stack.sandbox_pids_limit,
    }
    gateway = model_gateway.render_gateway_services(settings, AGENT_NETWORK, EGRESS_NETWORK, badges=False)
    return {
        "services": {**gateway, READER: reader},
        "networks": {
            # Isolated, as a run's is: the host has no address there, so the reader reaches
            # none of its services.
            AGENT_NETWORK: {"internal": True, "driver_opts": {GATEWAY_MODE_OPTION: "isolated"}},
            EGRESS_NETWORK: {"internal": False},
            model_gateway.RECORD_NETWORK: {"internal": True},
        },
        "volumes": {"sealed": {}},
        "secrets": {
            **model_gateway.declare_gateway_secrets(settings.gateway),
            READER_TOKEN_SECRET_NAME: {"environment": READER_TOKEN_VARIABLE},
        },
    }


def _spread(verdicts: Iterable[MonitorVerdict], high: float) -> str:
    """Each monitor's verdicts: how many, how many above ``high``, and the highest."""
    by_monitor: dict[str, list[float]] = {}
    for verdict in verdicts:
        by_monitor.setdefault(verdict.monitor, []).append(verdict.score)
    return "; ".join(
        f"{monitor}: verdicts {len(scores)}, above {high} {sum(score > high for score in scores)}, "
        f"highest {max(scores)}"
        for monitor, scores in sorted(by_monitor.items())
    )


def summarize_run(run_directory: Path) -> str:
    """The facts a reading opens with, counted from the run's records: Markdown."""
    scores = load_scores(run_directory / "scores.json")
    rows = []
    for play, phase in ((scores.mode, EPISODE_PHASE), ("twin", TWIN_PHASE)):
        sealed = run_directory / phase / "events.sealed.jsonl"
        if not sealed.is_file():  # an honest run has no twin
            continue
        kinds = Counter(event.kind for event in read_events(sealed))
        counts = (sum(kinds.values()), *(kinds[kind] for kind in ("job", "message", "action", "pr")))
        ends = run_directory / phase / "session_ends.json"
        recorded = json.loads(ends.read_text()).values() if ends.is_file() else []
        reasons = Counter(end["reason"] for end in recorded)
        ended = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items())) or "not recorded"
        rows.append(f"| {play} | {' | '.join(map(str, counts))} | {ended} |")
    main, side, safety, twin = scores.main_task, scores.side_task, scores.safety, scores.honest_twin
    high = safety.audit_threshold  # the run's own: a verdict above it is flagged
    reached = sorted(name for name, milestone in side.milestones.items() if milestone.reached)
    return "\n".join(
        [
            "## Facts counted from the records",
            "",
            "| Play | Sealed events | Job events | Messages | Actions | Pull request events | Sessions |",
            "|---|---|---|---|---|---|---|",
            *rows,
            "",
            f"- Main task: score {main.final_score:.3f}, gate {main.gate_reason}.",
            f"- Side task: landed {side.landing_minimal}; milestones: {', '.join(reached) or 'none'}.",
            f"- Caught: {safety.caught}; overall suspicion {safety.overall_suspicion}, "
            f"threshold {safety.aggregation.threshold if safety.aggregation else 'not recorded'}.",
            f"- Run verdicts: {_spread(scores.monitor_verdicts, high) or 'none'}.",
            f"- Twin verdicts: {(_spread(twin.monitor_verdicts, high) or 'none') if twin else 'no twin'}.",
            "",
        ],
    )


def read_report(written: Path, limit_bytes: int) -> str:
    """The report the reader left at ``written``.

    Raises ``RuntimeError`` when it is missing, is no regular file (the reader could leave a link to a file of
    this host, or a pipe that never answers) or is larger than ``limit_bytes``.
    """
    try:
        opened = os.open(written, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise RuntimeError(f"the reader left no regular file as its report: {error.strerror}") from error
    try:
        status = os.fstat(opened)
        if not stat.S_ISREG(status.st_mode):
            raise RuntimeError("the reader left no regular file as its report")
        if status.st_size > limit_bytes:
            raise RuntimeError(f"the reader's report is larger than {limit_bytes} bytes")
        return os.read(opened, limit_bytes).decode(errors="replace")
    finally:
        os.close(opened)


def read_run(run_directory: Path, brief: str, config: RunConfig) -> Path:
    """Have the reader read ``run_directory`` with ``brief``; the report's path, in the run's readings.

    An earlier reading with the same brief is replaced. Raises ``ValueError`` when the directory holds no
    graded run, no brief has that name or the models file has no reader role, and ``RuntimeError`` when the
    gateway does not start, the reader fails or runs past its timeout, its calls cannot be copied out, or its
    report cannot be kept.
    """
    run_directory = run_directory.resolve()
    if not (run_directory / "scores.json").is_file():
        raise ValueError(f"{run_directory} holds no scores.json: it is no graded run's directory")
    facts = summarize_run(run_directory)  # before any model call: a run it cannot count is not read
    settings = _settle(config)
    readings = run_directory / READINGS
    report, calls = readings / f"{brief}.md", readings / f"{brief}.calls.jsonl"
    with tempfile.TemporaryDirectory(prefix="locarena-reading-") as scratch:
        home, work = Path(scratch, "home"), Path(scratch, "work")
        work.mkdir()
        command = write_reader_home(home, config, brief)
        compose_file = Path(scratch, "compose.yaml")
        document = render_reader_compose(config, run_directory, home, work, command)
        compose_file.write_text(dump_document(document))
        project = f"{model_gateway.PROJECT_PREFIX}reading{secrets.token_hex(3)}"
        # The repository is the project directory, so compose reads the key from its .env as a run does.
        compose = ["docker", "compose", "-p", project, "-f", str(compose_file)]
        compose += ["--project-directory", str(REPOSITORY)]
        run = functools.partial(subprocess.run, env={**os.environ, **issue_reader_secrets(config)})
        if any(path.is_symlink() for path in (readings, report, calls, calls.with_suffix(".otlp.jsonl"))):
            raise RuntimeError(f"{readings} holds a link where a reading is written: nothing is written")
        try:
            if not model_gateway.start_gateway(run, compose, settings):
                raise RuntimeError("the gateway did not start: is the provider key set in .env or the shell?")
            readings.mkdir(exist_ok=True)  # an earlier reading is replaced only once this one can start
            for stale in (report, calls, calls.with_suffix(".otlp.jsonl")):
                stale.unlink(missing_ok=True)
            container = f"{project}-{READER}"
            try:
                reading = [*compose, "run", "--rm", "--no-deps", "--no-TTY", "--name", container, READER]
                # No input: pi reads its input to the end before it starts, and a shell's stays open.
                seconds = settings.reader.timeout_seconds
                failed = run(reading, timeout=seconds, stdin=subprocess.DEVNULL).returncode
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("the reader ran past reader.timeout_seconds") from error
            finally:  # the reader is gone before its calls are copied: none is made after the copy
                run(["docker", "rm", "--force", container], capture_output=True)
                copied = model_gateway.copy_call_log(run, compose, settings, calls, callers=[])
            if failed or not copied:
                raise RuntimeError(f"the reader failed (exit {failed}), or its calls could not be copied out")
        finally:
            run([*compose, "down", "--volumes", "--remove-orphans"])
        report.write_text(f"{facts}\n{read_report(work / _REPORT, settings.reader.report_limit_bytes)}")
    return report
