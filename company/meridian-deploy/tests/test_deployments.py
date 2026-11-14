"""The registry's own rules: ``deploy`` starts a deployment and returns, and the deployment ends by itself.

A service takes one deployment at a time, and a bounded number, each for a bounded time. The forge and the
supervisor are stand-ins of the registry's protocols; a real forge, IAM and supervisors are in the stack's
test (``tests/integration/test_deploy_stack.py``).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from queue import Queue

import pytest
from pydantic import SecretStr

from meridian_deploy.deployments import DeployError, Deployment, Deployments, MainTree, Outcome

WAIT_SECONDS = 10


@dataclass
class FakeCredentials:
    refused: bool = False

    def issue(self, reference: str, purpose: str) -> SecretStr:
        if self.refused:
            raise DeployError("you hold no right to read the forge, which a deployment fetches from")
        return SecretStr(f"credential of {reference} to {purpose}")


@dataclass
class FakeForge:
    """A forge whose one repo's ``main`` moves to a new commit before each fetch, unless it ``stands``."""

    stands: bool = False
    breaks: bool = False
    hangs: bool = False  # each fetch waits until ``answers`` is set
    answers: threading.Event = field(default_factory=threading.Event)
    fetched: int = 0

    def fetch(self, credential: SecretStr) -> dict[str, MainTree]:
        if self.hangs:
            self.answers.wait()
        if self.breaks:
            raise OSError(f"could not run git with {credential.get_secret_value()}")
        self.fetched += 0 if self.stands and self.fetched else 1
        return {"meridian-probe": MainTree(f"commit-{self.fetched}", b"")}


@dataclass
class FakeSupervisors:
    """Supervisors that serve every release; a hand-over waits until ``proceed`` is set."""

    proceed: threading.Event = field(default_factory=threading.Event)
    handed_over: threading.Event = field(default_factory=threading.Event)
    answers: bool = True

    def hand_over(self, service: str, release: str, archives: Mapping[str, bytes]) -> Outcome:
        if not self.answers:
            raise DeployError(f"{service}'s supervisor did not answer: ConnectError")
        self.handed_over.set()
        self.proceed.wait()
        return Outcome(release, serving=True, detail="")


@dataclass
class Registry:
    """A registry of notes' deployments, and each deployment it recorded: as accepted, then as it ended."""

    forge: FakeForge = field(default_factory=FakeForge)
    supervisors: FakeSupervisors = field(default_factory=FakeSupervisors)
    credentials: FakeCredentials = field(default_factory=FakeCredentials)
    recorded: Queue[Deployment] = field(default_factory=Queue)
    seconds: float = WAIT_SECONDS  # how long a deployment may last

    def __post_init__(self) -> None:
        collaborators = (self.credentials, self.forge, self.supervisors)
        self.deployments = Deployments(["notes"], *collaborators, 2, self.seconds, self.recorded.put)

    def wait_for_end(self) -> Deployment:
        """The next deployment the registry records as it ended."""
        while (recorded := self.recorded.get(timeout=WAIT_SECONDS)).state == "started":
            pass
        return recorded

    def deploy_to_its_end(self) -> Deployment:
        """A deployment of notes an agent starts, as the registry recorded it once it had ended."""
        self.supervisors.proceed.set()
        self.deployments.deploy("an-agent", "notes")
        return self.wait_for_end()


@pytest.fixture
def busy() -> Iterator[tuple[Registry, Deployment]]:
    """A registry with a deployment of notes in its hand-over, which ends when the test does."""
    registry = Registry()
    started = registry.deployments.deploy("an-agent", "notes")
    try:
        assert registry.supervisors.handed_over.wait(WAIT_SECONDS)
        yield registry, started
    finally:
        registry.supervisors.proceed.set()
        registry.wait_for_end()


def test_deploy_returns_a_started_deployment_while_its_release_is_handed_over(
    busy: tuple[Registry, Deployment],
) -> None:
    registry, started = busy

    listed = registry.deployments.list_deployments("notes")

    assert (started.number, started.by, started.state, started.commits) == (1, "an-agent", "started", {})
    assert listed == [started]


def test_a_deployment_is_recorded_as_it_is_accepted_before_its_release_is_fetched(hung: Registry) -> None:
    started = hung.deployments.deploy("an-agent", "notes")

    recorded = hung.recorded.get(timeout=WAIT_SECONDS)

    assert (recorded, recorded.state, hung.forge.fetched) == (started, "started", 0)


def test_a_service_with_a_deployment_in_progress_takes_no_other(busy: tuple[Registry, Deployment]) -> None:
    registry, _ = busy

    with pytest.raises(DeployError, match="deployment 1 of notes is in progress: ask list_deployments"):
        registry.deployments.deploy("another-agent", "notes")


def test_a_deployment_its_service_serves_on_is_listed_and_recorded_with_its_commits() -> None:
    registry = Registry()

    ended = registry.deploy_to_its_end()

    assert (ended.number, ended.state, ended.commits) == (1, "serving", {"meridian-probe": "commit-1"})
    assert registry.deployments.list_deployments("notes") == [ended]


def test_a_service_takes_no_deployment_past_its_bound() -> None:
    registry = Registry()
    registry.deploy_to_its_end()
    registry.deploy_to_its_end()

    with pytest.raises(DeployError, match="notes has had its 2 deployments"):
        registry.deployments.deploy("an-agent", "notes")

    assert [deployment.number for deployment in registry.deployments.list_deployments("notes")] == [2, 1]


def test_a_deployment_that_finds_nothing_to_deploy_ends_failed_saying_so() -> None:
    registry = Registry(forge=FakeForge(stands=True))
    registry.deploy_to_its_end()

    again = registry.deploy_to_its_end()

    assert (again.number, again.state, again.detail) == (
        2,
        "failed",
        "nothing to deploy: notes serves these commits already",
    )


def test_a_hand_over_no_supervisor_answers_is_a_failed_deployment_saying_so() -> None:
    registry = Registry(supervisors=FakeSupervisors(answers=False))

    ended = registry.deploy_to_its_end()

    assert (ended.state, ended.detail, registry.deployments.list_deployments("notes")) == (
        "failed",
        "notes's supervisor did not answer: ConnectError",
        [ended],
    )


def test_a_deployment_an_unforeseen_error_ends_is_failed_and_says_the_errors_kind_alone() -> None:
    registry = Registry(forge=FakeForge(breaks=True))

    ended = registry.deploy_to_its_end()

    assert (ended.state, ended.detail) == ("failed", "the deployment ended on an error: OSError")


def test_a_deployment_refused_its_credential_is_none_and_leaves_its_service_free() -> None:
    registry = Registry(credentials=FakeCredentials(refused=True))
    with pytest.raises(DeployError, match="you hold no right to read the forge"):
        registry.deployments.deploy("an-agent", "notes")
    registry.credentials.refused = False

    ended = registry.deploy_to_its_end()

    assert (ended.state, registry.deployments.list_deployments("notes")) == ("serving", [ended])


@pytest.fixture
def hung() -> Iterator[Registry]:
    """A registry whose forge answers no fetch until the test ends, and whose deployments last 0.2 s."""
    registry = Registry(forge=FakeForge(hangs=True), seconds=0.2)
    yield registry
    registry.forge.answers.set()


def test_a_deployment_that_outlasts_its_bound_ends_failed_saying_so_and_frees_its_service(
    hung: Registry,
) -> None:
    hung.deployments.deploy("an-agent", "notes")

    ended = hung.wait_for_end()
    after = hung.deployments.deploy("an-agent", "notes")

    assert (ended.number, ended.state, ended.detail) == (1, "failed", "the deployment did not end in 0.2 s")
    assert (after.number, after.state) == (2, "started")


def test_a_release_fetched_after_its_deployment_was_given_up_is_not_handed_over(hung: Registry) -> None:
    hung.deployments.deploy("an-agent", "notes")
    hung.wait_for_end()

    hung.forge.answers.set()  # the fetch answers, late

    assert not hung.supervisors.handed_over.wait(1)


def test_a_deployment_no_thread_starts_for_ends_failed_saying_so_and_frees_its_service() -> None:
    registry = Registry()
    usual = threading.stack_size(1 << 48)  # a stack larger than the address space: no thread starts
    try:
        unstarted = registry.deployments.deploy("an-agent", "notes")
    finally:
        threading.stack_size(usual)

    recorded = registry.wait_for_end()
    after = registry.deploy_to_its_end()

    assert (unstarted.state, unstarted.detail) == ("failed", "the deployment could not be started")
    assert (recorded, after.state) == (unstarted, "serving")
