"""The events monitors and the verifier read, built after play from each service's record of its tool calls.

Each call to a live service with tools is one record (``loc_arena.tool_records``): in process, in
``records/<service>.jsonl`` of the episode's directory; in a stack run, among the lines of the service's log
that start with ``{``, copied to ``services/<service>.log`` in the run directory. A record belongs to the
episode, or the honest twin, whose play window holds its time. Its events take the episode clock's reading at
that time and the native turn of its agent running then, else turn 0, and are written after every event of
play to the sealed log, and to the mirror log unless sealed-only. A call its service refused (it carries
``error.type``) makes a blocked ``action`` with the service's reason. A successful call makes zero or more
events: chat's ``post``, ``reply`` and ``send_dm`` a ``message``, over MCP or HTTP alike; the forge's
``open_pr`` a ``pr`` and an ``action``. A live service with rights and no tools records a request in its log
as a line naming its operation (``ServiceRequestRecord``); its other lines are not records. A request it
refused makes a blocked ``action``, and one that changed something an ``action``; a read it answered makes
none. A service with tools and rights both (the eval, tickets, notes and chat services) records a tool call or
a request per line, and each line is read as what it names. The job service, reached both ways, writes both
kinds of record, and each change of a job's state is a ``job`` event of its owner; the process facts each node
of its fleet writes (``services/node-<n>.log``) are ``process`` events on the sealed log alone. Neither comes
from an agent's call, so either may fall outside every turn, unnoted. A job's own calls carry its workload
principal (``job-<id>``), which is no agent of the run: each record a workload made is its sponsor's, by IAM's
``workload_issued`` audit line, and each of its events names the ``workload``; it too may fall outside every
turn, unnoted. A seeded job, another team's work the job service starts with, is marked ``seed`` on its
``job`` events, and its owner is a principal of the company: each record of one is placed as an agent's is,
its events of the actor role ``company``, in turn 0. Each line of ``unattributed_records.jsonl`` in the run
directory is a record and the ``reason`` it was not placed: not a valid record, outside every phase window, or
not an agent of the run; or no covering turn, a native agent's record outside all its turns, whose events are
built in turn 0. Grading never builds, so grading a run again adds nothing.
"""

from __future__ import annotations

import bisect
import contextlib
import dataclasses
import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPMethod, HTTPStatus
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError
from sandbox_server.confinement import resolve_inside
from scenarios.loader import LiveService

from loc_arena.config import RunConfig
from loc_arena.forge.service import OpenedPullRequest
from loc_arena.iam_seed import load_seed
from loc_arena.logging_.agent_trace import EpisodeTrace
from loc_arena.logging_.events import AppendOnlyLog, EventKind, read_events
from loc_arena.node_pool import list_node_services
from loc_arena.scaffold.bus import Recorder
from loc_arena.task import SNAPSHOT_FILE, SnapshotFile, read_snapshot_file, resolve_scenario
from loc_arena.tool_records import ToolRecord

_LOGGER = logging.getLogger(__name__)
SERVICE_LOGS = "services"  # a stack run's copy of each live service's log, in the run directory
RECORDS = "records"  # where an in-process run's services record, in each episode's directory
UNATTRIBUTED_FILE = "unattributed_records.jsonl"
COMPANY_ROLE = "company"  # the actor role of a company principal's events: no agent of the run
_OPENED_PULL_REQUEST = TypeAdapter(OpenedPullRequest)
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class ServiceRequestRecord(BaseModel):
    """A request a live service with rights recorded: its caller, its operation and arguments, its status."""

    model_config = ConfigDict(frozen=True)

    time: AwareDatetime = Field(validation_alias="wall_ts")  # without its UTC offset, in the wrong window
    caller: str | None  # none when the request carried no known identity
    method: str
    status: int
    operation: str
    arguments: dict[str, JsonValue]
    decision: str | None = None  # the IAM decision the request was made under, when the service has IAM


class _RecordedJob(BaseModel):
    """The fields of a job the job service's record holds that its ``job`` event carries."""

    id: str
    name: str
    owner: str
    principal: str | None = None  # the identity the job runs as
    schedule_id: str | None = None  # the schedule that started it, if one did
    seed: bool = False  # another team's work, on the cluster from its seed
    state: str
    node: str | None = None
    attempt: int = 1  # which run of it: one more each time a drain took it off its node
    exit_code: int | None = None


class JobStateRecord(BaseModel):
    """A change of a job's state the job service recorded, with the job as it was then."""

    model_config = ConfigDict(frozen=True)

    time: AwareDatetime = Field(validation_alias="wall_ts")
    job: _RecordedJob


class _ProcessFact(BaseModel):
    """What a node's daemon knew of its job's process: where, which phase, whose, and how it ended."""

    node: str
    phase: str
    job_id: str
    owner: str
    attempt: int = 1
    argv: list[str] | None = None
    exit_code: int | None = None


class ProcessRecord(BaseModel):
    """A process fact a node's daemon wrote: its job's start or its exit."""

    model_config = ConfigDict(frozen=True)

    time: AwareDatetime = Field(validation_alias="wall_ts")
    process: _ProcessFact


class _IssuedWorkload(BaseModel):
    """Whose work a workload is: its principal and its sponsor, the caller that launched the work."""

    principal: str
    sponsor: str


class WorkloadIssuedRecord(BaseModel):
    """IAM's audit line for a workload a service issued, naming its sponsor."""

    kind: Literal["workload_issued"]
    result: _IssuedWorkload


@dataclass(frozen=True)
class RecordedEvent:
    """One event a record makes, as its readers see it; its time, turn and actor's role come from the run."""

    kind: EventKind
    actor_uid: str
    target_id: str | None
    payload: dict[str, Any]
    tool: str | None = None
    result: dict[str, Any] | None = None
    to_mirror: bool = True  # false for a sealed-only event the monitors never see


def build_pull_request(record: ToolRecord, agent_ids: Sequence[str]) -> list[RecordedEvent]:
    """The ``pr`` event of the pull request the forge opened, with the fields the PR review monitor reads.

    Raises ``ValidationError`` when the record's result is not the forge's ``open_pr`` result.
    """
    _ = agent_ids
    opened = _OPENED_PULL_REQUEST.validate_python(record.result)
    return [
        RecordedEvent(
            "pr",
            record.agent,
            opened["repo"],
            {
                "number": opened["number"],
                "target_repo": opened["repo"],
                "pr_title": opened["title"],
                "pr_body": str(record.arguments.get("body", "")),
                "diff": str(record.arguments.get("diff", "")),
                "ci": opened["ci"],
            },
        ),
    ]


def chat_message(
    author: str,
    operation: str,
    arguments: Mapping[str, JsonValue],
    stored: Mapping[str, JsonValue] | None = None,
) -> RecordedEvent:
    """The ``message`` event of a post, reply or direct message chat took, as the action monitor reads it.

    Its target is the channel or the member; ``stored``, the message chat returned (an MCP call's result),
    adds the channel and thread a reply's arguments do not name.
    """
    stored = stored or {}
    thread = stored.get("thread", arguments.get("message"))
    channel = stored.get("channel", arguments.get("channel"))
    member = arguments.get("member")
    payload = {
        "message_kind": operation,
        "body": str(arguments.get("body", "")),
        "channel": channel,
        "thread": thread,
        "dm": operation == "send_dm",
    }
    target = member if isinstance(member, str) else channel if isinstance(channel, str) else None
    return RecordedEvent("message", author, target, payload)


def build_message(record: ToolRecord, agent_ids: Sequence[str]) -> list[RecordedEvent]:
    """The ``message`` event of a chat post an agent made over MCP."""
    _ = agent_ids
    return [chat_message(record.agent, record.tool, record.arguments, record.result)]


CHAT_POSTS: frozenset[str] = frozenset({"post", "reply", "send_dm"})
"""Chat's operations that write a message, each a ``message`` event, whether over MCP or HTTP."""

# What each tool's successful call makes beyond its action event, by tool name (unique across services under
# the tools gateway's prefixMode: never). A builder raises ValidationError on a result that is not its tool's.
EVENT_BUILDERS: Mapping[str, Callable[[ToolRecord, Sequence[str]], list[RecordedEvent]]] = {
    "open_pr": build_pull_request,
    **dict.fromkeys(CHAT_POSTS, build_message),
}


# The tools whose successful call is also an action (the eval, tickets and notes services' changes among them;
# their reads are none), and the argument naming what a call or request addresses.
_ACTIONS = {
    *("open_pr", "register_eval", "retire_eval", "start_run", "publish_run"),
    *("create_project", "edit_project", "create_ticket", "edit_ticket", "comment_on_ticket"),
    *("write_note", "delete_note"),
    "edit_message",
}
_TARGET_FROM: Mapping[str, str] = {
    "open_pr": "repo",
    "post": "channel",
    "send_dm": "member",
    "register_agent": "name",
    "update_agent": "name",
    "retire_agent": "name",
    "grant_rights": "agent",
    "revoke_rights": "agent",
    "start_execution": "agent",
    **dict.fromkeys(("cancel_execution", "stop_execution", "resume_execution"), "execution_id"),
    "submit_job": "name",
    **dict.fromkeys(("cancel_job", "hold_job", "release_job", "operate_job"), "job_id"),
    **dict.fromkeys(("delete_schedule", "operate_schedule"), "schedule_id"),
    "drain_node": "node_id",
    **dict.fromkeys(("register_eval", "retire_eval", "start_run"), "eval_id"),
    **dict.fromkeys(("bind_job", "report_results", "publish_run"), "run_id"),
    "create_project": "key",
    **dict.fromkeys(("edit_project", "create_ticket"), "project"),
    **dict.fromkeys(("get_ticket", "edit_ticket", "comment_on_ticket"), "ticket"),
    **dict.fromkeys(("read_note", "write_note", "delete_note"), "key"),
}
# The job service's tools that only read: an answered call of one makes no event, of any other an action.
_JOB_READS = frozenset(
    {"list_jobs", "get_job", "job_logs", "list_schedules", "get_schedule", "list_nodes", "usage"},
)


def _action_event(record: ToolRecord) -> RecordedEvent:
    """The dual ``action`` event the action monitor and the explorer read; blocked if the service refused."""
    target = record.arguments.get(_TARGET_FROM.get(record.tool, ""))
    target = target if isinstance(target, str) else None
    return RecordedEvent(
        "action",
        record.agent,
        target,
        {
            "args": record.arguments,
            "target": target,
            "blocked": record.error is not None,
            "reason": record.status_description,
        },
        tool=record.tool,
        result=dict(record.result) if record.result is not None else None,
    )


def build_events_from_service_request(record: ServiceRequestRecord) -> list[RecordedEvent]:
    """The ``action`` of a request its service refused or that changed something; a read it answered, none.

    A chat post it took is a ``message`` instead, as the same post over MCP is.
    """
    refused = record.status >= HTTPStatus.BAD_REQUEST
    if record.caller is None or (record.method == HTTPMethod.GET and not refused):
        return []
    if not refused and record.operation in CHAT_POSTS:
        return [chat_message(record.caller, record.operation, record.arguments)]
    target = record.arguments.get(_TARGET_FROM.get(record.operation, ""))
    target = target if isinstance(target, str) else None
    payload = {
        "args": record.arguments,
        "target": target,
        "blocked": refused,
        "reason": HTTPStatus(record.status).phrase if refused else None,
    }
    return [RecordedEvent("action", record.caller, target, payload, tool=record.operation)]


def build_events(record: ToolRecord, agent_ids: Sequence[str]) -> list[RecordedEvent]:
    """Every event ``record`` makes: a refused call's blocked action, else its tool's events and action."""
    if record.error is not None:
        return [_action_event(record)]
    builder = EVENT_BUILDERS.get(record.tool)
    events = builder(record, agent_ids) if builder is not None else []
    return [*events, _action_event(record)] if record.tool in _ACTIONS else events


@dataclass(frozen=True)
class _Phase:
    """One played episode: its directory, its snapshot, and the native turns of its trace."""

    directory: Path
    snapshot: SnapshotFile
    trace: EpisodeTrace | None

    def holds(self, wall: float) -> bool:
        """Whether ``wall`` falls inside this episode's play window."""
        clock, finished = self.snapshot.clock, self.snapshot.finished_at
        return bool(clock) and finished is not None and clock[0].wall <= wall <= finished

    def read_clock(self, wall: float) -> float:
        """The episode time at ``wall``: the clock's last reading at or before it."""
        walls = [reading.wall for reading in self.snapshot.clock]
        return self.snapshot.clock[bisect.bisect_right(walls, wall) - 1].ts

    def find_turn(self, agent: str, wall: float) -> int | None:
        """The turn of ``agent`` running at ``wall``: 0 if it has no native turn, none if none ran."""
        turns = [turn for turn in self.trace.turns if turn.ref.agent_uid == agent] if self.trace else []
        covering = (turn.ref.turn for turn in turns if turn.wall_start <= wall <= turn.wall_end)
        return next(covering, None if turns else 0)


@dataclass(frozen=True)
class _ParsedRecordLine:
    """A record line, parsed: when it was made, by whom, and the events it makes."""

    wall: float
    agent: str | None
    events: list[RecordedEvent]
    asynchronous: bool = False  # made by no agent's call (a job's process ending): outside a turn is expected


RecordLineParser = Callable[[str, Sequence[str]], _ParsedRecordLine | None]


def _parse_tool_call_line(line: str, agent_ids: Sequence[str]) -> _ParsedRecordLine:
    """A tool call's record; raises ``ValidationError`` for a line that is none."""
    record = ToolRecord.model_validate_json(line)
    return _ParsedRecordLine(record.time.timestamp(), record.agent, build_events(record, agent_ids))


def _parse_tool_call_or_request_line(line: str, agent_ids: Sequence[str]) -> _ParsedRecordLine | None:
    """A service's line, tools and rights both: a tool call's record if it names a tool, else a request's."""
    if "gen_ai.tool.name" in _JSON_OBJECT.validate_json(line):
        return _parse_tool_call_line(line, agent_ids)
    return _parse_service_request_line(line, agent_ids)


_IAM_MUTATIONS = frozenset(
    {
        "access_requested",
        "access_approved",
        "access_denied",
        "grant",
        "revoke",
        "member_added",
        "member_removed",
        "principal_disabled",
    },
)
_IAM_TARGETS = ("principal", "resource", "team", "request")


def _iam_line_parser(recorded_by_service: frozenset[str], approvers: frozenset[str]) -> RecordLineParser:
    """How a line of central IAM's audit log becomes events.

    An authorization decision is evidence to join to a resource's own record by its decision ID, never an
    action of its own: the resource's record is the action, and counting both would count it twice. A
    management mutation (a grant, a decision, a membership change, a request) is an action of the agent that
    made it, refused ones included, unless a service that writes its own operation record forwarded it (that
    record is the action, so IAM's event is the duplicate). A mutation nobody else recorded would otherwise be
    invisible to the monitors. A decision of a service's ``approvers`` (principals of the company) is no
    agent's action, and expected: it is neither placed nor reported unplaced.
    """

    def parse(line: str, agent_ids: Sequence[str]) -> _ParsedRecordLine | None:
        _ = agent_ids
        event = _JSON_OBJECT.validate_json(line)
        kind, blocked = event.get("kind"), False
        if kind == "rejected":
            kind, blocked = event.get("operation"), True
        actor, at, via = event.get("actor"), event.get("at"), event.get("via")
        # Every field is untrusted JSON: only strings are looked up in a set, so no line can abort the run.
        if not isinstance(kind, str) or kind not in _IAM_MUTATIONS or not isinstance(actor, str):
            return None
        if isinstance(at, bool) or not isinstance(at, int | float):
            return None
        if (isinstance(via, str) and via in recorded_by_service) or actor in approvers:
            return None
        found = next((value for key in _IAM_TARGETS if isinstance(value := event.get(key), str)), None)
        args = {k: v for k, v in event.items() if k not in {"at", "actor", "kind", "via", "result"}}
        payload = {
            "args": args,
            "target": found,
            "blocked": blocked,
            "reason": event.get("code") if blocked else None,
        }
        # What IAM answered (a new request's ID, a decision's requester) joins this event to the others.
        answer = event.get("result")
        result = answer if isinstance(answer, dict) else None
        return _ParsedRecordLine(
            float(at),
            actor,
            [RecordedEvent("action", actor, found, payload, tool=f"iam_{kind}", result=result)],
        )

    return parse


def _parse_job_service_line(line: str, agent_ids: Sequence[str]) -> _ParsedRecordLine | None:
    """A line of the job service: a job's change of state, a tool call, or an HTTP request.

    A change of state is a ``job`` event of the job's owner on both logs. A call that changes something
    (``submit_job``, ``drain_node``, any but a read) or that the service refused is an ``action``, as is a
    request over HTTP that did; a read it answered makes none.
    """
    fields = _JSON_OBJECT.validate_json(line)
    if "job" in fields:
        record = JobStateRecord.model_validate_json(line)
        job = record.job
        payload = {
            "job_id": job.id,
            "name": job.name,
            "principal": job.principal,
            "schedule_id": job.schedule_id,
            "seed": job.seed,
            "state": job.state,
            "node": job.node,
            "attempt": job.attempt,
            "exit_code": job.exit_code,
        }
        event = RecordedEvent("job", job.owner, job.id, payload)
        return _ParsedRecordLine(record.time.timestamp(), job.owner, [event], asynchronous=True)
    if "gen_ai.tool.name" in fields:
        call = ToolRecord.model_validate_json(line)
        changed = call.error is not None or call.tool not in _JOB_READS
        return _ParsedRecordLine(call.time.timestamp(), call.agent, [_action_event(call)] if changed else [])
    return _parse_service_request_line(line, agent_ids)


def _parse_process_line(line: str, agent_ids: Sequence[str]) -> _ParsedRecordLine:
    """A node's process fact: a ``process`` event of the job's owner on the sealed log alone."""
    _ = agent_ids
    record = ProcessRecord.model_validate_json(line)
    fact = record.process
    event = RecordedEvent(
        "process",
        fact.owner,
        fact.job_id,
        fact.model_dump(exclude_none=True),
        to_mirror=False,
    )
    return _ParsedRecordLine(record.time.timestamp(), fact.owner, [event], asynchronous=True)


def _parse_service_request_line(line: str, agent_ids: Sequence[str]) -> _ParsedRecordLine | None:
    """A request's record, or ``None`` for a line naming no operation; ``ValidationError`` for a bad one."""
    _ = agent_ids
    if "operation" not in _JSON_OBJECT.validate_json(line):
        return None
    record = ServiceRequestRecord.model_validate_json(line)
    return _ParsedRecordLine(
        record.time.timestamp(),
        record.caller,
        build_events_from_service_request(record),
    )


@dataclass(frozen=True)
class _Placed:
    """A record's events, placed: its episode, its wall time, its agent's turn, and the events themselves."""

    phase: _Phase
    wall: float
    events: list[RecordedEvent]
    turn: int


def build_recorded_events(
    run_directory: Path,
    config: RunConfig,
    traces: Mapping[str, EpisodeTrace | None],
) -> None:
    """Write the events of every record of the run's services into the episode it was made in.

    ``traces`` holds each episode's trace by the name of its directory in ``run_directory``; an episode that
    did not play is skipped.
    """
    phases = [
        _Phase(run_directory / name, read_snapshot_file(run_directory / name), trace)
        for name, trace in traces.items()
        if (run_directory / name / SNAPSHOT_FILE).exists()
    ]
    roles = {agent.id: agent.kind for agent in config.agents}
    agent_ids = tuple(roles)
    roles |= dict.fromkeys(_read_company_principals(run_directory, config, phases) - set(roles), COMPANY_ROLE)
    sponsors = _read_sponsors(run_directory, config, phases)
    record_line_parsers: list[tuple[str, RecordLineParser]] = [
        *(
            (service.name, _attribute_workloads(_record_line_parser(service, config), sponsors))
            for service in config.live_services
            if service.tools or service.rights
        ),
        *((node, _attribute_workloads(_parse_process_line, sponsors)) for node in list_node_services(config)),
    ]
    unattributed: list[str] = []
    built: dict[Path, list[_Placed]] = {phase.directory: [] for phase in phases}
    for service, parse_record_line in record_line_parsers:
        for line in _read_record_lines(run_directory, service, phases):
            placed, reason = _place(line, parse_record_line, phases, roles, agent_ids)
            unattributed += [json.dumps({"reason": reason, "line": line})] if reason else []
            if placed:
                built[placed.phase.directory].append(placed)
    for phase in phases:
        _write_events(phase, sorted(built[phase.directory], key=lambda placed: placed.wall), roles)
    if unattributed:
        _LOGGER.warning("%d service records were not placed: see %s", len(unattributed), UNATTRIBUTED_FILE)
        (run_directory / UNATTRIBUTED_FILE).write_text("".join(f"{note}\n" for note in unattributed))


def _record_line_parser(service: LiveService, config: RunConfig) -> RecordLineParser:
    """How a service's log lines become events: a tool call, a request, or (central IAM) correlation only."""
    if config.settings.iam.enabled and service.name == config.settings.iam.service:
        seed = load_seed(resolve_scenario(config))
        resources = {} if seed is None else seed.resources
        recorded = frozenset(name for name, resource in resources.items() if resource.records_actions)
        approvers = frozenset(resource.approver.principal for resource in resources.values())
        return _iam_line_parser(recorded, approvers)
    if service.nodes:  # the job service: its tools, its HTTP API and its jobs' changes of state
        return _parse_job_service_line
    if service.tools and service.rights:
        return _parse_tool_call_or_request_line
    return _parse_tool_call_line if service.tools else _parse_service_request_line


def _read_sponsors(run_directory: Path, config: RunConfig, phases: list[_Phase]) -> dict[str, str]:
    """Each workload central IAM issued in the run, by principal, and the agent its work is for.

    A workload another workload launched works for that one's sponsor: IAM logs a workload's issuance after
    its sponsor's, so each sponsor is resolved by the time its workloads are read.
    """
    sponsors: dict[str, str] = {}
    if not config.settings.iam.enabled:
        return sponsors
    for line in _read_record_lines(run_directory, config.settings.iam.service, phases):
        try:
            issued = WorkloadIssuedRecord.model_validate_json(line).result
        except ValidationError:  # IAM's every other audit line
            continue
        sponsors[issued.principal] = sponsors.get(issued.sponsor, issued.sponsor)
    return sponsors


def _read_company_principals(run_directory: Path, config: RunConfig, phases: list[_Phase]) -> set[str]:
    """The principals of the company whose records are placed beside the agents': each seeded job's owner."""
    owners: set[str] = set()
    for service in (service.name for service in config.live_services if service.nodes):
        for line in _read_record_lines(run_directory, service, phases):
            with contextlib.suppress(ValidationError):  # its other lines: its requests and tool calls
                job = JobStateRecord.model_validate_json(line).job
                owners |= {job.owner} if job.seed else set()
    return owners


def _attribute_workloads(parse: RecordLineParser, sponsors: Mapping[str, str]) -> RecordLineParser:
    """``parse``, with each record a workload made attributed to the agent it works for.

    Each of the workload's events becomes its sponsor's, naming the ``workload``, and like a job's, may fall
    outside every turn of the sponsor's.
    """

    def parse_attributed(line: str, agent_ids: Sequence[str]) -> _ParsedRecordLine | None:
        record = parse(line, agent_ids)
        workload = None if record is None else record.agent
        if record is None or workload is None or workload not in sponsors:
            return record
        sponsor = sponsors[workload]
        events = [
            dataclasses.replace(event, actor_uid=sponsor, payload={**event.payload, "workload": workload})
            if event.actor_uid == workload
            else event
            for event in record.events
        ]
        return _ParsedRecordLine(record.wall, sponsor, events, asynchronous=True)

    return parse_attributed


def _read_record_lines(run_directory: Path, service: str, phases: list[_Phase]) -> Iterator[str]:
    """A service's record lines: those of its log in a stack run, else of each episode's records file."""
    stack_log = run_directory / SERVICE_LOGS / f"{service}.log"
    in_process = [phase.directory / RECORDS / f"{service}.jsonl" for phase in phases]
    for path in (path for path in [stack_log, *in_process] if path.exists()):
        # A line not starting with "{" is one the service logged around its records, such as uvicorn's.
        lines = path.read_text(encoding="utf-8").splitlines()
        yield from (line for line in lines if line.startswith("{"))


def _place(
    line: str,
    parse_record_line: RecordLineParser,
    phases: list[_Phase],
    roles: Mapping[str, str],
    agent_ids: Sequence[str],
) -> tuple[_Placed | None, str | None]:
    """The events of a record ``line``, placed, if it makes any; and why not, or not in a turn."""
    try:
        record = parse_record_line(line, agent_ids)
    except ValidationError:
        return None, "not a valid record"
    if record is None:
        return None, None
    phase = next((phase for phase in phases if phase.holds(record.wall)), None)
    if phase is None or record.agent is None or record.agent not in roles:
        return None, "outside every phase window" if phase is None else "not an agent of the run"
    if not record.events:
        return None, None
    turn = phase.find_turn(record.agent, record.wall)
    outside = turn is None and not record.asynchronous
    return _Placed(phase, record.wall, record.events, turn or 0), "no covering turn" if outside else None


def _write_events(phase: _Phase, built: list[_Placed], roles: Mapping[str, str]) -> None:
    """Append ``built`` to the logs of ``phase``, after every event its play wrote."""
    if not built:
        return
    sealed_path = resolve_inside(phase.directory, phase.snapshot.sealed_path)
    episode_id = next(read_events(sealed_path)).episode_id
    sealed = AppendOnlyLog(sealed_path, episode_id)
    mirror = AppendOnlyLog(resolve_inside(phase.directory, phase.snapshot.mirror_path), episode_id)
    recorder = Recorder(episode_id, sealed, mirror)
    for placed in built:
        ts = phase.read_clock(placed.wall)
        for event in placed.events:
            write = recorder.dual if event.to_mirror else recorder.sealed
            write(
                actor_uid=event.actor_uid,
                actor_role=roles[event.actor_uid],
                kind=event.kind,
                payload=event.payload,
                target_id=event.target_id,
                turn=placed.turn,
                tool=event.tool,
                result=event.result,
                ts=ts,
            )
