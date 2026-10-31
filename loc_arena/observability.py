"""Meridian's observability store in a stack run: Phoenix, where the company's traced executions report spans.

A scenario has it as its live service ``phoenix``, open to everything on agent-net; runners reach it at
``PHOENIX_COLLECTOR_ENDPOINT``. Phoenix keeps its spans in its container, which the honest twin's renewal
recreates empty, so each play's spans are exported once its runners have stopped, before anything is
recreated: added, one JSON line each as Phoenix's REST API lists them, to ``services/phoenix.spans.jsonl`` in
the run directory. Phoenix is open to every agent, so one may crash it: a Phoenix that is not running holds no
spans the company can read and exports none, and one that stops during the export keeps those it listed first.
The side-task checker reads the export for the model calls it names.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, JsonValue, ValidationError
from scenarios.loader import LiveService

from loc_arena.config import RunConfig

_LOGGER = logging.getLogger(__name__)
PHOENIX_SERVICE = "phoenix"
PHOENIX_ENDPOINT_VARIABLE = "PHOENIX_COLLECTOR_ENDPOINT"
PHOENIX_PYTHON = "python3.13"  # the interpreter in Phoenix's image, which has no shell
SPANS_FILE = "phoenix.spans.jsonl"
RESPONSE_ID_ATTRIBUTE = "gen_ai.response.id"  # the provider's id of the reply a model call's span traces
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


class _ExportedSpan(BaseModel):
    attributes: dict[str, JsonValue]


class SpansNotExportedError(RuntimeError):
    """The export failed while Phoenix still runs: its spans are still only in its container."""


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

    An export that fails while Phoenix still runs is a fault of the export, and raises
    ``SpansNotExportedError``.
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
        raise SpansNotExportedError(f"the export of Phoenix's spans failed: {done.stderr.decode()}")
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


def read_traced_response_ids(service_logs: Path) -> frozenset[str]:
    """The provider's reply ids the spans exported to ``service_logs`` name: the model calls Phoenix holds.

    Phoenix takes spans from anyone, so one nested deeper or holding a longer number than pydantic reads is
    left out.
    """
    spans_file = service_logs / SPANS_FILE
    lines = spans_file.read_text(encoding="utf-8").splitlines() if spans_file.exists() else []
    response_ids = set()
    for line in lines:
        try:
            span = _ExportedSpan.model_validate_json(line)
        except ValidationError as error:
            if any(detail["type"] != "json_invalid" for detail in error.errors()):
                raise
            _LOGGER.warning("Left out a span pydantic cannot read: %s", error.errors()[0]["msg"])
            continue
        response_id = span.attributes.get(RESPONSE_ID_ATTRIBUTE)
        if isinstance(response_id, str):
            response_ids.add(response_id)
    return frozenset(response_ids)
