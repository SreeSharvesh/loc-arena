"""The lines a service writes in its record besides its tool calls, read back.

A request a live service with rights answered, a change of a job's state the job service made, a process fact
a node's daemon wrote, and central IAM's audit line for a workload it issued. ``loc_arena.recorded_events``
builds events from them, and the side-task verifier reads them as they are.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue


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


class RecordedJob(BaseModel):
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
    job: RecordedJob


class _ProcessFact(BaseModel):
    """What a node's daemon knew of its job's process: where, which phase, whose, and how it ended."""

    node: str
    phase: str
    job_id: str
    owner: str
    attempt: int = 1
    argv: list[str] | None = None
    checkout: dict[str, str] | None = None  # at its start, the commit of each repo's main it runs, by repo
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
