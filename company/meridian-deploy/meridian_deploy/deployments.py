"""The deployments of each service: who released which commits to it, and how each ended.

A deployment builds a release, every company repo's ``main`` by commit, and hands it to one service's
supervisor, which says whether the service then serves on it. ``deploy`` starts one and returns: the
deployment runs to its end in a thread of its own, and its caller asks ``list_deployments`` how it ended.
The registry is in memory. What it needs of its deployment it takes as protocols: a credential for each
deployment, the forge's ``main`` read with it, and the hand-over to a service. A refused deployment raises
``DeployError`` and is no deployment; one that starts and cannot fetch, finds nothing to deploy, or whose
hand-over fails is a failed deployment. Each call that is given a number counts toward the service's bound.
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal, NamedTuple, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, SecretStr

DEPLOY = "deploy"  # the launch operation each deployment's credential is issued through


class DeployError(Exception):
    """A deployment the service refuses, with the reason its caller reads."""


class Deployment(BaseModel):
    """One deployment: the release handed to a service, by whom, and whether the service serves on it."""

    model_config = ConfigDict(frozen=True)

    number: int
    service: str
    by: str
    at: AwareDatetime  # when it started
    commits: dict[str, str]  # each company repo's commit of main, by repo; none until they are fetched
    state: Literal["started", "serving", "failed"]  # started: in progress
    detail: str = ""  # of a failed one: why the service did not serve on it


class MainTree(NamedTuple):
    """One repo's ``main``: its commit, and its tree as a tar."""

    commit: str
    archive: bytes


class Outcome(NamedTuple):
    """What a supervisor answered a hand-over: the release its service runs, and whether it serves."""

    release: str | None
    serving: bool
    detail: str


class Credentials(Protocol):
    """Issues each deployment a credential of its own on the forge, of what its caller holds."""

    def issue(self, reference: str, purpose: str) -> SecretStr:
        """The credential of deployment ``reference``; raises ``DeployError`` when it is refused."""
        ...


class Source(Protocol):
    """The forge, read with a deployment's credential."""

    def fetch(self, credential: SecretStr) -> dict[str, MainTree]:
        """Each company repo's ``main``; raises ``DeployError`` when the forge refuses or does not answer."""
        ...


class Rollout(Protocol):
    """The hand-over of a release to a service's supervisor."""

    def hand_over(self, service: str, release: str, archives: Mapping[str, bytes]) -> Outcome:
        """Have ``service`` run ``release``; raises ``DeployError`` when its supervisor does not answer."""
        ...


class Deployments:
    """Every deployment of the services this one deploys to."""

    def __init__(  # noqa: PLR0913 - what a deployment is made with
        self,
        services: Sequence[str],
        credentials: Credentials,
        source: Source,
        rollout: Rollout,
        max_deployments: int,
        record: Callable[[Deployment], None] = lambda ended: None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Deploy to ``services`` alone, each asked ``max_deployments`` times at most.

        ``record`` takes each deployment as it ended, in the context of the call that started it; ``clock``
        dates it.
        """
        self._credentials, self._source, self._rollout = credentials, source, rollout
        self._max_deployments, self._record, self._clock = max_deployments, record, clock
        self._history: dict[str, list[Deployment]] = {service: [] for service in services}
        self._asked = dict.fromkeys(services, 0)  # every call that got as far as a number, refused or not
        self._in_progress = {service: threading.Lock() for service in services}
        self._numbers = threading.Lock()
        self._last_number = 0

    def deploy(self, caller: str, service: str) -> Deployment:
        """Start releasing the forge's ``main`` to ``service`` for ``caller``; the deployment, as started."""
        history = self._find_history(service)
        if not self._in_progress[service].acquire(blocking=False):
            listed = history and history[-1].state == "started"  # not yet, while its credential is issued
            current = f"deployment {history[-1].number}" if listed else "a deployment"
            raise DeployError(
                f"{current} of {service} is in progress: "
                "ask list_deployments how it ended, then deploy again",
            )
        try:
            if self._asked[service] >= self._max_deployments:
                raise DeployError(f"{service} has had its {self._max_deployments} deployments")
            self._asked[service] += 1
            with self._numbers:
                self._last_number += 1
                number = self._last_number
            credential = self._credentials.issue(f"{DEPLOY}-{number}", f"deploy {service}")
            started = Deployment(
                number=number,
                service=service,
                by=caller,
                at=self._clock(),
                commits={},
                state="started",
            )
            history.append(started)
            # In its caller's context: what the call holds, its credential among it, lasts the deployment.
            run = contextvars.copy_context().run
            threading.Thread(target=run, args=(self._carry_out, started, credential), daemon=True).start()
        except BaseException:
            self._in_progress[service].release()
            raise
        return started

    def _carry_out(self, started: Deployment, credential: SecretStr) -> None:
        """Fetch ``started``'s release, hand it over, and keep and record the deployment as it ended."""
        history, release = self._history[started.service], f"{DEPLOY}-{started.number}"
        commits: dict[str, str] = {}
        try:
            trees = self._source.fetch(credential)
            commits = {repo: tree.commit for repo, tree in trees.items()}
            serving = next((past for past in reversed(history) if past.state == "serving"), None)
            if serving is not None and serving.commits == commits:
                raise DeployError(f"nothing to deploy: {started.service} serves these commits already")
            archives = {repo: tree.archive for repo, tree in trees.items()}
            outcome = self._rollout.hand_over(started.service, release, archives)
            served = outcome.serving and outcome.release == release
            detail = "" if served else outcome.detail or "the service did not answer on the release"
        except DeployError as refused:  # a hand-over nobody answered among them: it did not deploy
            served, detail = False, str(refused)
        except Exception as error:  # noqa: BLE001 - whatever ended it, the deployment ends, and failed
            # Its kind alone: its text may hold what a reader of the deployments may not read.
            served, detail = False, f"the deployment ended on an error: {type(error).__name__}"
        ended = started.model_copy(
            update={"commits": commits, "state": "serving" if served else "failed", "detail": detail},
        )
        history[-1] = ended  # the service's last: it takes no other while this one is in progress
        self._in_progress[started.service].release()
        self._record(ended)

    def list_deployments(self, service: str) -> list[Deployment]:
        """The deployments of ``service``, newest first."""
        return list(reversed(self._find_history(service)))

    def _find_history(self, service: str) -> list[Deployment]:
        if service not in self._history:
            deployed = ", ".join(sorted(self._history))
            raise DeployError(f"{service} is deployed by its owners; this service deploys {deployed}")
        return self._history[service]
