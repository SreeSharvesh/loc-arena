"""The deployments of each service: who released which commits to it, and how each ended.

A deployment builds a release, every company repo's ``main`` by commit, and hands it to one service's
supervisor, which says whether the service then serves on it. The registry is in memory. What it needs of
its deployment it takes as protocols: a credential for each deployment, the forge's ``main`` read with it,
and the hand-over to a service. A refused deployment raises ``DeployError`` and changes nothing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal, NamedTuple, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict

DEPLOY = "deploy"  # the launch operation each deployment's credential is issued through


class DeployError(Exception):
    """A deployment the service refuses, with the reason its caller reads."""


class Deployment(BaseModel):
    """One deployment: the release handed to a service, by whom, and whether the service serves on it."""

    model_config = ConfigDict(frozen=True)

    number: int
    service: str
    by: str
    at: AwareDatetime
    commits: dict[str, str]  # each company repo's commit of main, by repo
    state: Literal["serving", "failed"]
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

    def issue(self, reference: str, purpose: str) -> str:
        """The credential of deployment ``reference``; raises ``DeployError`` when it is refused."""
        ...


class Source(Protocol):
    """The forge, read with a deployment's credential."""

    def fetch(self, credential: str) -> dict[str, MainTree]:
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
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Deploy to ``services`` alone, at most ``max_deployments`` times each; ``clock`` dates each."""
        self._credentials, self._source, self._rollout = credentials, source, rollout
        self._max_deployments, self._clock = max_deployments, clock
        self._history: dict[str, list[Deployment]] = {service: [] for service in services}
        self._under_way = {service: threading.Lock() for service in services}
        self._numbers = threading.Lock()
        self._last_number = 0

    def deploy(self, caller: str, service: str) -> Deployment:
        """Release the forge's ``main`` to ``service`` for ``caller``; the deployment, as it ended."""
        history = self._find_history(service)
        if not self._under_way[service].acquire(blocking=False):
            raise DeployError(f"a deployment of {service} is under way")
        try:
            if len(history) >= self._max_deployments:
                raise DeployError(f"{service} has had its {self._max_deployments} deployments")
            with self._numbers:
                self._last_number += 1
                number = self._last_number
            release = f"{DEPLOY}-{number}"
            trees = self._source.fetch(self._credentials.issue(release, f"deploy {service}"))
            commits = {repo: tree.commit for repo, tree in trees.items()}
            serving = next((past for past in reversed(history) if past.state == "serving"), None)
            if serving is not None and serving.commits == commits:
                raise DeployError(f"nothing to deploy: {service} serves these commits already")
            archives = {repo: tree.archive for repo, tree in trees.items()}
            outcome = self._rollout.hand_over(service, release, archives)
            served = outcome.serving and outcome.release == release
            deployment = Deployment(
                number=number,
                service=service,
                by=caller,
                at=self._clock(),
                commits=commits,
                state="serving" if served else "failed",
                detail="" if served else outcome.detail or "the service did not answer on the release",
            )
            history.append(deployment)
            return deployment
        finally:
            self._under_way[service].release()

    def list_deployments(self, service: str) -> list[Deployment]:
        """The deployments of ``service``, newest first."""
        return list(reversed(self._find_history(service)))

    def _find_history(self, service: str) -> list[Deployment]:
        if service not in self._history:
            deployed = ", ".join(sorted(self._history))
            raise DeployError(f"{service} is deployed by its owners; this service deploys {deployed}")
        return self._history[service]
