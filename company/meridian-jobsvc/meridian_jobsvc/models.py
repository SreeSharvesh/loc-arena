"""What the job service takes and answers: a job to run, a job as it is, its logs, the nodes and the usage.

A job's request carries everything its submitter decides; the service adds whose the job is, the identity it
runs as, the team its CPU-hours count against, where it was placed and how each attempt went. A job's
``environment`` is on its record, so it holds no secret: the job's credentials are its own (``authority``).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from meridian_common.jobclient.models import JobSpec, JobState

MAX_COMMAND_CHARACTERS = 32_000  # every argument of a command together
CLUSTER_VARIABLE_PREFIX = "MERIDIAN_"  # the variables the cluster sets: a job's credentials and the gateway
NODE_VARIABLES = frozenset({"HOME", "PATH", "TMPDIR"})  # the variables each node sets for its job


def _limit_command(command: list[str]) -> list[str]:
    if sum(map(len, command)) > MAX_COMMAND_CHARACTERS:
        raise ValueError(f"the command's arguments hold more than {MAX_COMMAND_CHARACTERS} characters")
    return command


def _refuse_cluster_variables(environment: dict[str, str]) -> dict[str, str]:
    taken = sorted(
        name for name in environment if name.startswith(CLUSTER_VARIABLE_PREFIX) or name in NODE_VARIABLES
    )
    if taken:
        raise ValueError(f"the cluster sets {taken} itself: name the job's variables otherwise")
    return environment


JobName = Annotated[str, Field(min_length=1, max_length=200, description="what the job does, for people")]
Command = Annotated[
    list[str],
    Field(min_length=1, description="the program and its arguments, run in the job's own empty workspace"),
    AfterValidator(_limit_command),
]
Cpu = Annotated[float, Field(gt=0, description="the cpus it needs: its node gives it at least that many")]
MemoryMb = Annotated[int, Field(gt=0, description="the memory it needs, in MB")]
Priority = Annotated[int, Field(description="among jobs ready to start, a higher one starts first")]
DependsOn = Annotated[list[str], Field(description="ids of jobs that must succeed before this one starts")]
Labels = Annotated[dict[str, str], Field(max_length=32, description="labels for people and tools")]
VariableName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")]
Environment = Annotated[
    dict[VariableName, Annotated[str, Field(max_length=4096)]],
    Field(
        max_length=64,
        description="variables the job's process gets beside its credentials: on its record",
    ),
    AfterValidator(_refuse_cluster_variables),
]


class JobRequest(BaseModel):
    """A job to run: its command, the cpu and memory it needs, its priority, what it waits for, its labels."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: JobName
    command: Command
    cpu: Cpu = 1.0
    memory_mb: MemoryMb = 512
    priority: Priority = 0
    depends_on: DependsOn = Field(default_factory=list)
    labels: Labels = Field(default_factory=dict)
    environment: Environment = Field(default_factory=dict)


class Job(BaseModel):
    """One job: what it asked for, whose it is, the identity it runs as, where it was placed, how it went."""

    id: str
    name: str
    command: list[str]
    cpu: float
    memory_mb: int
    priority: int
    depends_on: list[str]
    labels: dict[str, str]
    environment: dict[str, str] = Field(default_factory=dict)
    owner: str
    principal: str | None  # the job's own identity, which its calls carry, never its owner's: none if seeded
    team: str  # whose CPU-hours it uses: its sponsor's team
    schedule_id: str | None = None  # the schedule that started it, if one did
    seed: bool = False  # on the cluster from its seed: another team's work, not submitted to the service
    state: JobState
    node: str | None = None
    attempt: int = 1  # which run of it this is: one more each time a drain takes it off its node
    submitted_at: datetime
    started_at: datetime | None = None  # its current attempt's start
    ended_at: datetime | None = None
    exit_code: int | None = None  # negative for the signal that ended its process
    cpu_hours: float = 0.0  # its cpus times the wall time its attempts ran
    message: str = ""


def spec_of(job: JobRequest | Job) -> JobSpec:
    """What the queue takes of ``job``: what it asks for, its priority and what it waits for."""
    return JobSpec(
        name=job.name,
        command=job.command,
        cpu=job.cpu,
        memory_mb=job.memory_mb,
        priority=job.priority,
        depends_on=tuple(job.depends_on),
        labels=job.labels,
        environment=job.environment,
    )


class Jobs(BaseModel):
    """Every job, oldest first."""

    jobs: list[Job]


class JobLogs(BaseModel):
    """The tails of a job's stdout and stderr, of its current attempt: empty for a job not running yet."""

    job_id: str
    stdout: str
    stderr: str


class NodeState(StrEnum):
    """Whether a node takes a job: free, busy with one, draining it, or drained and taking none."""

    FREE = "free"
    BUSY = "busy"
    DRAINING = "draining"
    DRAINED = "drained"


class ClusterNode(BaseModel):
    """One node of the fleet: the cpu and memory a job placed on it gets, its state, and its job."""

    id: str
    cpu: float
    memory_mb: int
    state: NodeState
    job_id: str | None = None


class Nodes(BaseModel):
    """Every node of the fleet."""

    nodes: list[ClusterNode]


class Usage(BaseModel):
    """The CPU-hours the cluster's jobs have used, from the wall time they ran: by team, and by job."""

    teams: dict[str, float]
    jobs: dict[str, float]
