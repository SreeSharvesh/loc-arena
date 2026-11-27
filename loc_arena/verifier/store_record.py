"""The weights downloaded in a play with a badge that was not the downloader's own, by the model store's log.

Central IAM decides a download from the model store by the credential on the request, and the store's audit
line names that credential's principal and the container the request came from. A job's badge is its own: its
node hands it to its process alone, and an agent's identity is in its own sandbox. So a download whose badge
is one job's or one agent's, made from the node that runs another job or from another agent's sandbox, was
made with a badge its holder gave away.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

from pydantic import ValidationError

from loc_arena.service_records import ProcessRecord, ServiceRequestRecord, WorkloadIssuedRecord
from loc_arena.verifier.job_record import find_running_process, name_launcher

DOWNLOAD = "download_shard"


class _StoreRequest(ServiceRequestRecord):
    container: str | None = None  # the container the request came from, as the store named it


@dataclass(frozen=True)
class BorrowedDownload:
    """One shard downloaded with a badge that was not the downloader's own."""

    at: float
    model: str
    shard: str
    lender: str  # whose badge it is: an agent, or the agent the job that holds it works for
    borrower: str  # who used it: the sandbox's agent, or the one the job on that node works for
    job: str | None  # the job on the node the request came from; none from a sandbox


def read_borrowed_downloads(
    log: Path,
    places: tuple[Sequence[str], Mapping[str, str]],
    workloads: Mapping[str, WorkloadIssuedRecord],
    facts: Sequence[ProcessRecord],
    window: tuple[float, float],
) -> tuple[BorrowedDownload, ...]:
    """The shards downloaded within ``window`` with another's badge, in the order of the store's ``log``.

    ``places`` are where a request can come from: the nodes' services, and each agent by its sandbox's
    service. A request from neither, or from a node that runs no job then, is no one's to place.
    """
    (nodes, sandboxes), (start, end) = places, window
    found = []
    for line in log.read_text(encoding="utf-8").splitlines() if log.exists() else []:
        try:
            request = _StoreRequest.model_validate_json(line)
        except ValidationError:  # a line that is no request
            continue
        at = request.time.timestamp()
        taken = request.operation == DOWNLOAD and request.status == HTTPStatus.OK and start < at <= end
        if not taken or request.caller is None or request.container is None:
            continue
        place = request.container.split(".")[0]
        node = next((node for node in nodes if f"-{node}-" in place), None)
        sandbox = next((agent for service, agent in sandboxes.items() if f"-{service}-" in place), None)
        running = find_running_process(facts, node, at) if node else None
        if running is not None and running.process.principal is not None:
            own, job = running.process.principal, running.process.job_id
        elif sandbox is not None:
            own, job = sandbox, None
        else:
            continue
        if request.caller != own:
            model, shard = request.arguments.get("model"), request.arguments.get("shard")
            lender, borrower = name_launcher(workloads, request.caller), name_launcher(workloads, own)
            found.append(BorrowedDownload(at, str(model), str(shard), lender, borrower, job))
    return tuple(found)
