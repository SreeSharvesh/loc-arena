"""Meridian's observability store in a stack run: Phoenix, where the company's traced executions report spans.

A scenario has it as its live service ``phoenix``, open to everything on agent-net; runners reach it at
``PHOENIX_COLLECTOR_ENDPOINT``. Phoenix keeps its spans in its container, which the honest twin's renewal
recreates empty, so each play's spans are exported once its runners have stopped, before anything is
recreated: added, one JSON line each as Phoenix's REST API lists them, to ``services/phoenix.spans.jsonl`` in
the run directory. Phoenix is open to every agent, so one may crash it: a Phoenix that is not running holds no
spans the company can read and exports none, and one that stops during the export keeps those it listed first.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue
from scenarios.loader import LiveService

from loc_arena.config import RunConfig

_LOGGER = logging.getLogger(__name__)
PHOENIX_SERVICE = "phoenix"
PHOENIX_ENDPOINT_VARIABLE = "PHOENIX_COLLECTOR_ENDPOINT"
PHOENIX_PYTHON = "python3.13"  # the interpreter in Phoenix's image, which has no shell
SPANS_FILE = "phoenix.spans.jsonl"
Run = tuple[str, str, str]  # a run the agents service recorded: the execution, its runner, who started it
# Run in Phoenix's container, given its URL there: prints every span of every project, page by page, so no
# project an agent creates can push the company's off the first page. Phoenix builds each page in memory, and
# a span can hold a long conversation, so a page is a hundred spans, as Phoenix pages its projects.
EXPORT_SPANS = """
import json, sys, urllib.parse, urllib.request
api = f"{sys.argv[1]}/v1"
def list_all(path, **query):
    cursor = None
    while True:
        page_query = urllib.parse.urlencode({**query, **({"cursor": cursor} if cursor else {})})
        with urllib.request.urlopen(f"{api}{path}?{page_query}") as reply:
            page = json.load(reply)
        yield from page["data"]
        cursor = page["next_cursor"]
        if cursor is None:
            return
every_kind = {"include_experiment_projects": "true", "include_dataset_evaluator_projects": "true"}
for project in list(list_all("/projects", limit=100, **every_kind)):
    project_path = urllib.parse.quote(project["id"], safe="")
    for span in list_all(f"/projects/{project_path}/spans", limit=100, sort="id", order="asc"):
        print(json.dumps({"project": project["name"], **span}))
"""


class TracedSpan(BaseModel):
    """A span Phoenix holds, as its REST API lists it: what it reports, when, and its attributes.

    Phoenix takes spans from anyone, so an attribute holds whatever its sender wrote.
    """

    model_config = ConfigDict(frozen=True)

    project: str
    name: str
    span_kind: str
    start_time: AwareDatetime
    end_time: AwareDatetime
    status_code: str
    attributes: dict[str, JsonValue]

    @property
    def reported(self) -> dict[str, JsonValue]:
        """What the span reports: its name, kind and status, and its attributes."""
        return {"name": self.name, "kind": self.span_kind, "status": self.status_code, **self.attributes}

    @property
    def response_id(self) -> str | None:
        """The provider's id of the reply the span says it traces, if it says."""
        response_id = self.attributes.get("gen_ai.response.id")
        return response_id if isinstance(response_id, str) else None

    @property
    def execution(self) -> str | None:
        """The execution the span names, if it names one."""
        execution = self.attributes.get("meridian.execution.id")
        return execution if isinstance(execution, str) else None

    @property
    def started_by(self) -> str | None:
        """Who the span says started its execution, if it says."""
        started_by = self.attributes.get("meridian.execution.started_by")
        return started_by if isinstance(started_by, str) else None

    @property
    def runner(self) -> str | None:
        """The runner the span says its execution ran on, if it says."""
        runner = self.attributes.get("meridian.runner")
        return runner if isinstance(runner, str) else None


class ExecutionStateLine(BaseModel):
    """An audit line of the agents service: an execution's new state, its runner, and who started it."""

    model_config = ConfigDict(frozen=True)

    time: AwareDatetime = Field(validation_alias="wall_ts")
    execution: str
    state: str
    runner: str | None
    started_by: str


def find_phoenix(config: RunConfig) -> LiveService | None:
    """The scenario's Phoenix, if it has one."""
    return next((service for service in config.live_services if service.name == PHOENIX_SERVICE), None)


def export_spans(
    run_compose: Callable[..., subprocess.CompletedProcess[bytes]],
    compose: list[str],
    config: RunConfig,
    service_logs: Path,
) -> None:
    """Add every span Phoenix lists to its file in ``service_logs``: none without a Phoenix running.

    An export that fails while Phoenix still runs is a fault of the export, and raises.
    """
    phoenix = find_phoenix(config)
    if phoenix is None:
        return
    if not _is_phoenix_running(run_compose, compose):
        _LOGGER.warning("Phoenix is not running, so it holds no spans to export")
        return
    export = [*compose, "exec", "-T", PHOENIX_SERVICE, PHOENIX_PYTHON, "-c", EXPORT_SPANS]
    done = run_compose([*export, f"http://localhost:{phoenix.port}"], capture_output=True)
    listed = done.stdout[: done.stdout.rfind(b"\n") + 1]  # an export cut off can end inside a line
    with (service_logs / SPANS_FILE).open("ab") as spans:
        spans.write(listed)
    if done.returncode != 0 and _is_phoenix_running(run_compose, compose):
        raise subprocess.CalledProcessError(done.returncode, export, stderr=done.stderr)
    if done.returncode != 0:
        _LOGGER.warning(
            "Phoenix stopped during the export: the %d spans it listed first are kept",
            listed.count(b"\n"),
        )


def _is_phoenix_running(
    run_compose: Callable[..., subprocess.CompletedProcess[bytes]],
    compose: list[str],
) -> bool:
    """Whether the stack's Phoenix container is running."""
    running = [*compose, "ps", "--status", "running", "--quiet", PHOENIX_SERVICE]
    return bool(run_compose(running, capture_output=True, check=True).stdout.strip())


def read_execution_state_lines(service_log: Path) -> list[ExecutionStateLine]:
    """The execution audit lines among the lines of the agents service's log; none without the log."""
    lines = service_log.read_text(encoding="utf-8").splitlines() if service_log.exists() else []
    objects = [json.loads(line) for line in lines if line.startswith("{")]
    return [
        ExecutionStateLine.model_validate(line) for line in objects if "execution" in line and "state" in line
    ]


def read_runs(service_log: Path) -> frozenset[Run]:
    """Each run the agents service's audit lines record: the execution, its runner, and who started it."""
    return frozenset(
        (line.execution, line.runner, line.started_by)
        for line in read_execution_state_lines(service_log)
        if line.state == "running" and line.runner is not None
    )


def read_span_lines(service_logs: Path) -> list[str]:
    """The lines of every span exported to ``service_logs``; none when nothing was."""
    spans_file = service_logs / SPANS_FILE
    return spans_file.read_text(encoding="utf-8").splitlines() if spans_file.exists() else []


def read_spans(service_logs: Path) -> list[TracedSpan]:
    """Every span exported to ``service_logs``."""
    return [TracedSpan.model_validate_json(line) for line in read_span_lines(service_logs)]
