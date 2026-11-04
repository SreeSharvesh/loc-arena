"""The eval service's registry: Meridian's evals, the runs on them, their results and the leaderboards.

An eval is a named set of text items and the metrics a run of it reports; the first metric is its headline,
the score its leaderboard ranks by. A run is one eval of one model: who started it, the job its caller submits
to the cluster, the job it is bound to once that job reports in, and its results, appended to the versioned
``ResultsStore`` so a stored version never changes. The registry lives in memory and starts from a seed. A
refused change raises ``EvalServiceError`` with the HTTP status that says why; who may ask is the service's
concern.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt

from meridian_common.jobclient import JobSpec
from meridian_evalkit.leaderboard import Leaderboard, LeaderboardEntry
from meridian_evalkit.metrics import default_registry
from meridian_evalkit.store import EvalRun, ResultsStore, diff_runs

SEED_FILE = Path(__file__).with_name("seed.yaml")
EVAL_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
RUNNER_MODULE = "meridian_evalkit.run"  # the job's command: the runner, which reads its run from the service


class EvalServiceError(Exception):
    """A request the registry refuses, with the HTTP status that says why."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        """Refuse with ``status`` and a message the caller reads."""
        super().__init__(message)
        self.status = status


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class TextItem(_Body):
    """One item of an eval: the prompt the model is given and the reference its answer is scored against."""

    item_id: Annotated[str, Field(min_length=1)]
    prompt: str
    reference: str
    category: str = "default"


def _distinct_items(items: list[TextItem]) -> list[TextItem]:
    """``items``, each with an id of its own, so a result's item ids name one item each."""
    counted = Counter(item.item_id for item in items)
    if repeated := sorted(item_id for item_id, count in counted.items() if count > 1):
        raise ValueError(f"items repeat the ids {repeated}")
    return items


_Items = Annotated[list[TextItem], Field(min_length=1), AfterValidator(_distinct_items)]


class EvalRegistration(_Body):
    """A new eval: its id, its items and the metrics its runs report, the first being the headline."""

    eval_id: Annotated[str, Field(pattern=EVAL_ID_PATTERN)]
    items: _Items
    metrics: Annotated[list[str], Field(min_length=1)]


class Eval(BaseModel):
    """An eval: its items, its metrics, who registered it and when, and when it was retired, if it was."""

    model_config = ConfigDict(frozen=True)

    eval_id: str
    metrics: list[str]
    items: list[TextItem]
    registered_by: str
    registered_at: AwareDatetime
    retired_at: AwareDatetime | None = None

    @property
    def headline_metric(self) -> str:
        """The metric the eval's leaderboard ranks by: the first it declares."""
        return self.metrics[0]


class EvalSummary(BaseModel):
    """An eval in a listing: everything but its items, which ``get_eval`` serves."""

    model_config = ConfigDict(frozen=True)

    eval_id: str
    metrics: list[str]
    item_count: int
    registered_by: str
    registered_at: AwareDatetime
    retired_at: AwareDatetime | None = None


class RunStart(_Body):
    """What starts a run: the eval to run and the model to run it on."""

    eval_id: str
    model_id: str


class Results(_Body):
    """What a run's job reports: each metric's value and the ids of the items it scored."""

    metrics: dict[str, float]
    item_ids: Annotated[list[str], Field(min_length=1)]


class JobBody(BaseModel):
    """The job a run's caller submits to the cluster, as ``POST /jobs`` on the job service takes it."""

    model_config = ConfigDict(frozen=True)

    name: str
    command: list[str]
    cpu: float
    memory_mb: int
    priority: int
    depends_on: list[str]
    labels: dict[str, str]


class JobResources(BaseModel):
    """What an eval run's job asks of the cluster; settings of the deployment, never the caller's choice."""

    model_config = ConfigDict(frozen=True)

    cpu: PositiveFloat = 1.0
    memory_mb: PositiveInt = 2048


class RunState(StrEnum):
    """Where a run is: created and waiting for its job, started by that job, or reported by it."""

    CREATED = "created"
    STARTED = "started"
    REPORTED = "reported"


class Run(BaseModel):
    """One eval of one model: who started it, its job, the job bound to it, its state and its results."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    eval_id: str
    model_id: str
    owner: str
    state: RunState
    job: JobBody
    job_principal: str | None = None
    metrics: dict[str, float] | None = None
    results_version: int | None = None
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    reported_at: AwareDatetime | None = None
    published_at: AwareDatetime | None = None


class MetricChange(BaseModel):
    """One metric's change between two runs."""

    model_config = ConfigDict(frozen=True)

    name: str
    old: float
    new: float
    delta: float


class Diff(BaseModel):
    """The difference between two reported runs: metric deltas and the metrics and items added or removed."""

    model_config = ConfigDict(frozen=True)

    left: str
    right: str
    metric_deltas: list[MetricChange]
    added_metrics: list[str]
    removed_metrics: list[str]
    added_items: list[str]
    removed_items: list[str]


class RankedRun(BaseModel):
    """A published run on its eval's leaderboard: its rank, its model and its headline score."""

    model_config = ConfigDict(frozen=True)

    rank: int
    run_id: str
    model_id: str
    score: float


class Board(BaseModel):
    """An eval's leaderboard: its published runs ranked by the eval's headline metric, in its direction."""

    model_config = ConfigDict(frozen=True)

    eval_id: str
    metric: str
    higher_is_better: bool
    ranking: list[RankedRun]


class SeededEval(_Body):
    """An eval the registry starts with."""

    eval_id: Annotated[str, Field(pattern=EVAL_ID_PATTERN)]
    registered_by: str
    registered_at: AwareDatetime
    metrics: Annotated[list[str], Field(min_length=1)]
    items: _Items
    retired_at: AwareDatetime | None = None


class SeededRun(_Body):
    """A run the registry starts with: reported by the job it names; published if it says when."""

    run_id: str
    eval_id: str
    model_id: str
    owner: str
    job_principal: str
    created_at: AwareDatetime
    started_at: AwareDatetime
    reported_at: AwareDatetime
    metrics: dict[str, float]
    item_ids: list[str] | None = None  # every item of the eval when left out
    published_at: AwareDatetime | None = None


class Seed(_Body):
    """What a registry starts with: the models the company serves, its evals and their past runs."""

    models: Annotated[list[str], Field(min_length=1)]
    evals: list[SeededEval] = []
    runs: list[SeededRun] = []


def load_seed(seed_file: Path = SEED_FILE) -> Seed:
    """The seed in ``seed_file``."""
    return Seed.model_validate(yaml.safe_load(seed_file.read_text()))


class EvalkitRegistry:
    """The evals, runs, results and leaderboards, in memory. A refused change raises ``EvalServiceError``."""

    def __init__(
        self,
        seed: Seed,
        *,
        url: str = "http://evalkit:8000",
        resources: JobResources | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Start from ``seed``: ``url`` is where a run's job reaches this service, ``clock`` its times."""
        self._url = url
        self._resources = resources or JobResources()
        self._clock = clock
        self._metrics = default_registry()
        self._models = tuple(seed.models)
        self._evals: dict[str, Eval] = {}
        self._runs: dict[str, Run] = {}
        self._store = ResultsStore()
        self._boards: dict[str, Leaderboard] = {}
        self._next_run_number = 1
        for seeded_eval in seed.evals:
            self._add_eval(Eval(**seeded_eval.model_dump()))
        for seeded_run in seed.runs:
            self._add_seeded_run(seeded_run)

    @property
    def models(self) -> tuple[str, ...]:
        """The Meridian model names a run may name."""
        return self._models

    def list_evals(self) -> list[EvalSummary]:
        """Every eval, retired ones included, by id."""
        return [
            EvalSummary(**found.model_dump(exclude={"items"}), item_count=len(found.items))
            for _, found in sorted(self._evals.items())
        ]

    def get_eval(self, eval_id: str) -> Eval:
        """The eval ``eval_id``, items included."""
        try:
            return self._evals[eval_id]
        except KeyError:
            raise EvalServiceError(HTTPStatus.NOT_FOUND, f"no eval {eval_id!r}") from None

    def register_eval(self, caller: str, registration: EvalRegistration) -> Eval:
        """Register an eval as ``caller``; its id is new and its metrics are ones the harness computes."""
        if registration.eval_id in self._evals:
            raise EvalServiceError(HTTPStatus.CONFLICT, f"eval {registration.eval_id!r} exists")
        self._require_known_metrics(registration.metrics)
        return self._add_eval(
            Eval(**registration.model_dump(), registered_by=caller, registered_at=self._clock()),
        )

    def retire_eval(self, eval_id: str) -> Eval:
        """Retire an eval: no new runs; its runs and results stay."""
        found = self.get_eval(eval_id)
        if found.retired_at is not None:
            raise EvalServiceError(HTTPStatus.CONFLICT, f"eval {eval_id!r} is retired")
        self._evals[eval_id] = retired = found.model_copy(update={"retired_at": self._clock()})
        return retired

    def start_run(self, caller: str, start: RunStart) -> Run:
        """Record a run of ``start.eval_id`` on ``start.model_id`` for ``caller``, with the job to submit."""
        found = self.get_eval(start.eval_id)
        if found.retired_at is not None:
            raise EvalServiceError(HTTPStatus.CONFLICT, f"eval {start.eval_id!r} is retired")
        if start.model_id not in self._models:
            raise EvalServiceError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"no model {start.model_id!r}: the models are {', '.join(self._models)}",
            )
        run_id = f"run-{self._next_run_number:04d}"
        run = Run(
            run_id=run_id,
            eval_id=start.eval_id,
            model_id=start.model_id,
            owner=caller,
            state=RunState.CREATED,
            job=self._job_for(run_id, start.eval_id, start.model_id),
            created_at=self._clock(),
        )
        return self._add_run(run)

    def list_runs(self) -> list[Run]:
        """Every run, oldest first."""
        return list(self._runs.values())

    def get_run(self, run_id: str) -> Run:
        """The run ``run_id``."""
        try:
            return self._runs[run_id]
        except KeyError:
            raise EvalServiceError(HTTPStatus.NOT_FOUND, f"no run {run_id!r}") from None

    def bind_job(self, run_id: str, principal: str) -> Run:
        """Bind the job calling as ``principal`` to the run: the first to start it, and the only one."""
        run = self.get_run(run_id)
        if run.state is not RunState.CREATED:
            raise EvalServiceError(
                HTTPStatus.CONFLICT,
                f"run {run_id!r} is {run.state}: its job is {run.job_principal!r}",
            )
        update = {"state": RunState.STARTED, "job_principal": principal, "started_at": self._clock()}
        self._runs[run_id] = started = run.model_copy(update=update)
        return started

    def report_results(self, run_id: str, principal: str, results: Results) -> Run:
        """Append the results of the run's job, calling as ``principal``, as a new version; once."""
        run = self.get_run(run_id)
        if run.state is RunState.CREATED:
            raise EvalServiceError(HTTPStatus.CONFLICT, f"run {run_id!r} has no job yet")
        if principal != run.job_principal:
            raise EvalServiceError(
                HTTPStatus.FORBIDDEN,
                f"only the job of run {run_id!r} reports it: {run.job_principal!r}",
            )
        if run.state is RunState.REPORTED:
            raise EvalServiceError(
                HTTPStatus.CONFLICT,
                f"run {run_id!r} is reported: version {run.results_version}",
            )
        return self._record_results(run, results.metrics, results.item_ids, self._clock())

    def diff_runs(self, left_id: str, right_id: str) -> Diff:
        """The metric deltas and the metrics and items added or removed from ``left_id`` to ``right_id``."""
        diff = diff_runs(self._results_of(left_id), self._results_of(right_id))
        return Diff(
            left=diff.left,
            right=diff.right,
            metric_deltas=[
                MetricChange(name=change.name, old=change.old, new=change.new, delta=change.delta)
                for change in diff.metric_deltas
            ],
            added_metrics=list(diff.added_metrics),
            removed_metrics=list(diff.removed_metrics),
            added_items=list(diff.added_items),
            removed_items=list(diff.removed_items),
        )

    def publish_run(self, run_id: str) -> Run:
        """Put a reported run on its eval's leaderboard, scored by the eval's headline metric; once."""
        run = self.get_run(run_id)
        if run.published_at is not None:
            raise EvalServiceError(HTTPStatus.CONFLICT, f"run {run_id!r} is published")
        return self._publish(run, self._results_of(run_id), self._clock())

    def leaderboards(self, eval_id: str | None = None) -> list[Board]:
        """Each eval's leaderboard, or ``eval_id``'s alone; an eval with no published run has none."""
        if eval_id is not None:
            self.get_eval(eval_id)
        boards = []
        for found_id, board in sorted(self._boards.items()):
            if eval_id is not None and found_id != eval_id:
                continue
            metric = self._evals[found_id].headline_metric
            higher_is_better = self._metrics.get(metric).higher_is_better
            ranking = [
                RankedRun(
                    rank=rank,
                    run_id=entry.run_id,
                    model_id=entry.model,
                    score=entry.score if higher_is_better else -entry.score,
                )
                for rank, entry in enumerate(board.ranking(), start=1)
            ]
            boards.append(
                Board(eval_id=found_id, metric=metric, higher_is_better=higher_is_better, ranking=ranking),
            )
        return boards

    def _require_known_metrics(self, metrics: Iterable[str]) -> None:
        if unknown := sorted(set(metrics) - set(self._metrics.names())):
            raise EvalServiceError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"no metric {unknown}: the harness computes {', '.join(self._metrics.names())}",
            )

    def _add_eval(self, found: Eval) -> Eval:
        self._evals[found.eval_id] = found
        return found

    def _add_run(self, run: Run) -> Run:
        self._runs[run.run_id] = run
        number = run.run_id.removeprefix("run-")
        if number.isdigit():
            self._next_run_number = max(self._next_run_number, int(number) + 1)
        return run

    def _add_seeded_run(self, seeded: SeededRun) -> None:
        """A seeded run is what its job reported, and is on the leaderboard if the seed says when."""
        found = self.get_eval(seeded.eval_id)
        if seeded.model_id not in self._models:
            raise ValueError(f"seeded run {seeded.run_id!r} names no seeded model: {seeded.model_id!r}")
        run = self._add_run(
            Run(
                run_id=seeded.run_id,
                eval_id=seeded.eval_id,
                model_id=seeded.model_id,
                owner=seeded.owner,
                state=RunState.STARTED,
                job=self._job_for(seeded.run_id, seeded.eval_id, seeded.model_id),
                job_principal=seeded.job_principal,
                created_at=seeded.created_at,
                started_at=seeded.started_at,
            ),
        )
        item_ids = seeded.item_ids if seeded.item_ids is not None else [item.item_id for item in found.items]
        reported = self._record_results(run, seeded.metrics, item_ids, seeded.reported_at)
        if seeded.published_at is not None:
            self._publish(reported, self._results_of(reported.run_id), seeded.published_at)

    def _record_results(self, run: Run, metrics: dict[str, float], item_ids: list[str], at: datetime) -> Run:
        """Append ``metrics``, exactly the eval's, over ``item_ids``, the eval's items, as a new version."""
        found = self._evals[run.eval_id]
        if set(metrics) != set(found.metrics):
            raise EvalServiceError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"results report the metrics {sorted(metrics)}; the eval's are {found.metrics}",
            )
        if unknown := sorted(set(item_ids) - {item.item_id for item in found.items}):
            raise EvalServiceError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                f"results score items the eval has not: {unknown}",
            )
        results = EvalRun(
            run_id=run.run_id,
            model=run.model_id,
            metrics=dict(metrics),
            item_ids=tuple(item_ids),
        )
        version = self._store.append(results)
        update = {
            "state": RunState.REPORTED,
            "metrics": dict(metrics),
            "results_version": version,
            "reported_at": at,
        }
        self._runs[run.run_id] = reported = run.model_copy(update=update)
        return reported

    def _publish(self, run: Run, results: EvalRun, at: datetime) -> Run:
        """Rank the run on its eval's board by its headline score, negated where a lower score is better."""
        metric = self._evals[run.eval_id].headline_metric
        score = results.metrics[metric]
        entry = LeaderboardEntry(
            run_id=run.run_id,
            model=run.model_id,
            score=score if self._metrics.get(metric).higher_is_better else -score,
        )
        self._boards.setdefault(run.eval_id, Leaderboard()).add(entry)
        self._runs[run.run_id] = published = run.model_copy(update={"published_at": at})
        return published

    def _results_of(self, run_id: str) -> EvalRun:
        """The stored results of a reported run."""
        run = self.get_run(run_id)
        if run.results_version is None:
            raise EvalServiceError(HTTPStatus.CONFLICT, f"run {run_id!r} is not reported")
        return self._store.get(run.results_version)

    def _job_for(self, run_id: str, eval_id: str, model_id: str) -> JobBody:
        """The job that runs ``run_id``: the runner, from the checkout, on the run's model."""
        command = ["python", "-m", RUNNER_MODULE, "--evalkit", self._url, "--run", run_id]
        spec = JobSpec(
            name=f"eval {eval_id} {model_id}",
            command=[*command, "--model", model_id],
            cpu=self._resources.cpu,
            memory_mb=self._resources.memory_mb,
            labels={"run_id": run_id, "eval_id": eval_id, "model_id": model_id},
        )
        return JobBody.model_validate(spec.to_dict())
