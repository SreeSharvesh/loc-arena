"""The isolated reader process shared by Markdown readings and structured run insights."""

from __future__ import annotations

import functools
import json
import os
import secrets
import stat
import subprocess
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
from loc_arena.gateway import model_gateway
from loc_arena.gateway.loop_routes import LOOP_ROUTE_PREFIX, Loops
from loc_arena.own_sessions import PI_ENVIRONMENT, PI_SETTINGS
from loc_arena.settings import LocArenaSettings
from loc_arena.yaml_documents import dump_document

READER = "reader"  # its compose service, its name as the gateway's caller, and its role in the models file
READER_TOKEN_SECRET_NAME = "reader_token"
READER_TOKEN_VARIABLE = "LOC_ARENA_READER_TOKEN"
READINGS = "readings"  # in the run directory: each brief's report, and the reader's calls beside it
BRIEFS = Path(__file__).parent / "reader_briefs"
_DATA, _HOME, _WORK = "/data", "/home/reader", "/work"  # in the reader's container
ASK = "ask"  # the brief that answers one question
_FORMAT = BRIEFS / "_answer.md"  # what every brief shares: what the reader is given and what it writes
_ANSWER = "answer.json"
_PROVIDER = "gateway"
_ROUTE = f"{LOOP_ROUTE_PREFIX}/{READER}"  # the reader's own route at the gateway
_KICKOFF = "Read the run as your brief says, write /work/answer.json, then print it."


def settle_reading(config: RunConfig) -> LocArenaSettings:
    """A reading's settings: the run config's with one upstream, the one the reader calls, and its own relay.

    Its gateway tunnels through ``reader.outbound_proxy`` where that is set, so a reading's relay listens
    beside the relay of a play that runs on the same machine.
    """
    gateway = config.settings.gateway.model_copy(update={"agents_upstream": None})
    if (proxy := config.settings.reader.outbound_proxy) is not None:
        gateway = gateway.model_copy(update={"outbound_proxy": proxy})
    return config.settings.model_copy(update={"gateway": gateway})


def issue_reader_secrets(config: RunConfig) -> dict[str, str]:
    """A reading's secrets by host variable: the reader's token, and the gateway config that admits it.

    The token opens one route, the reader's own, as an agent's loop has: it bounds the reader's calls at
    ``reader.model_calls`` and sends each upstream as the reader role's model, whatever the call names. The
    route goes to ``gateway.upstream``, whatever upstream a run gave its agents' loops, and refuses a call
    that names another model or a list of fallback models.
    """
    role, settings = config.models[READER], settle_reading(config)
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
    loops = Loops(calls, {READER: overrides}, caller=(READER, SecretStr(token)), allow=allow)
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


def write_reader_home(home: Path, config: RunConfig, brief: str, question: str | None = None) -> list[str]:
    """Write the reader's home (pi's provider and settings, and ``brief``); return pi's command.

    The ``ask`` brief answers ``question``, and only it takes one. Raises ``ValueError`` when no brief has
    that name, or a question is missing or given to another brief.
    """
    names = sorted(path.stem for path in BRIEFS.glob("[!_]*.md"))
    if brief not in names:
        raise ValueError(f"no brief {brief!r}: the briefs are {', '.join(names)}")
    if (brief == ASK) != bool(question and question.strip()):
        raise ValueError(f"the {ASK!r} brief, and it alone, answers a question")
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
    shared = _FORMAT.read_text().replace("CLAIM_LIMIT", str(settings.reader.claim_limit))
    (home / "brief.md").write_text(f"{shared}\n{(BRIEFS / f'{brief}.md').read_text()}")
    kickoff = f"Answer this question as your brief says, write /work/answer.json, then print it: {question}"
    return [
        *["pi", "--print", "--model", f"{_PROVIDER}/{role.model}", "--tools", "read,bash,write"],
        *["--no-mcp", "--no-extensions", "--no-context-files", "--no-skills", "--no-session"],
        *["--append-system-prompt", f"{_HOME}/brief.md", kickoff if question else _KICKOFF],
    ]


def render_reader_compose(
    config: RunConfig,
    run_directory: Path,
    home: Path,
    work: Path,
    command: list[str],
) -> ComposeDocument:
    """The compose file of a reading: the gateway, the Collector, and the reader that runs ``command``."""
    settings = settle_reading(config)
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


def run_reader_process(
    config: RunConfig,
    run_directory: Path,
    home: Path,
    work: Path,
    command: list[str],
    calls: Path,
    replace: tuple[Path, ...] = (),
) -> str:
    """Run pi through its isolated gateway, record its calls, and read its bounded JSON answer.

    ``home`` and ``work`` are private temporary folders prepared by the caller. Existing ``replace``
    artifacts are removed only after the gateway starts. Every failure tears the stack down; a timeout
    also stops the reader before copying its call record.
    """
    settings = settle_reading(config)
    compose_file = home.parent / "compose.yaml"
    document = render_reader_compose(config, run_directory, home, work, command)
    compose_file.write_text(dump_document(document))
    project = f"{model_gateway.PROJECT_PREFIX}reading{secrets.token_hex(3)}"
    # The repository is the project directory, so compose reads the key from its .env as a run does.
    compose = ["docker", "compose", "-p", project, "-f", str(compose_file)]
    compose += ["--project-directory", str(REPOSITORY)]
    run = functools.partial(subprocess.run, env={**os.environ, **issue_reader_secrets(config)})
    readings = calls.parent
    artifacts = (*replace, calls, calls.with_suffix(".otlp.jsonl"))
    if any(path.is_symlink() for path in (readings, *artifacts)):
        raise RuntimeError(f"{readings} holds a link where a reading is written: nothing is written")
    try:
        if not model_gateway.start_gateway(run, compose, settings):
            raise RuntimeError("the gateway did not start: is the provider key set in .env or the shell?")
        readings.mkdir(exist_ok=True)  # an earlier reading is replaced only once this one can start
        for stale in artifacts:
            stale.unlink(missing_ok=True)
        container = f"{project}-{READER}"
        try:
            reading = [*compose, "run", "--rm", "--no-deps", "--no-TTY", "--name", container, READER]
            # No input: pi reads its input to the end before it starts, and a shell's stays open. Its
            # output goes unread: only its answer, once checked, reaches a person.
            seconds = settings.reader.timeout_seconds
            failed = run(
                reading,
                timeout=seconds,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
            ).returncode
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("the reader ran past reader.timeout_seconds") from error
        finally:  # the reader is gone before its calls are copied: none is made after the copy
            run(["docker", "rm", "--force", container], capture_output=True)
            copied = model_gateway.copy_call_log(run, compose, settings, calls, callers=[])
        if failed or not copied:
            raise RuntimeError(f"the reader failed (exit {failed}), or its calls could not be copied out")
    finally:
        run([*compose, "down", "--volumes", "--remove-orphans"])
    return read_report(work / _ANSWER, settings.reader.report_limit_bytes)
