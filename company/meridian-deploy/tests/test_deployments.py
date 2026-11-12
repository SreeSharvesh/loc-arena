"""The registry's own rules: a service takes one deployment at a time, and a bounded number.

The forge and the supervisor are stand-ins of the registry's protocols; a real forge, IAM and supervisors
are in the stack's test (``tests/integration/test_deploy_stack.py``).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pytest

from meridian_deploy.deployments import DeployError, Deployments, MainTree, Outcome


class FakeCredentials:
    def issue(self, reference: str, purpose: str) -> str:
        return f"credential of {reference} to {purpose}"


@dataclass
class FakeForge:
    """A forge whose one repo's ``main`` moves to a new commit before each fetch."""

    fetched: int = 0

    def fetch(self, credential: str) -> dict[str, MainTree]:
        self.fetched += 1
        return {"meridian-probe": MainTree(f"commit-{self.fetched}", b"")}


@dataclass
class FakeSupervisors:
    """Supervisors that serve every release; the first hand-over waits until ``proceed`` is set."""

    proceed: threading.Event = field(default_factory=threading.Event)
    handed_over: threading.Event = field(default_factory=threading.Event)

    def hand_over(self, service: str, release: str, archives: Mapping[str, bytes]) -> Outcome:
        first = not self.handed_over.is_set()
        self.handed_over.set()
        if first:
            self.proceed.wait()
        return Outcome(release, serving=True, detail="")


def test_a_service_takes_no_deployment_past_its_bound() -> None:
    supervisors = FakeSupervisors()
    supervisors.proceed.set()
    deployments = Deployments(["notes"], FakeCredentials(), FakeForge(), supervisors, max_deployments=2)
    deployments.deploy("an-agent", "notes")
    deployments.deploy("an-agent", "notes")

    with pytest.raises(DeployError, match="notes has had its 2 deployments"):
        deployments.deploy("an-agent", "notes")

    assert [deployment.number for deployment in deployments.list_deployments("notes")] == [2, 1]


@pytest.fixture
def busy() -> Iterator[Deployments]:
    """A registry with a deployment of notes under way, which ends when the test does."""
    supervisors = FakeSupervisors()
    deployments = Deployments(["notes"], FakeCredentials(), FakeForge(), supervisors, max_deployments=5)
    with ThreadPoolExecutor() as pool:
        pool.submit(deployments.deploy, "an-agent", "notes")
        supervisors.handed_over.wait()
        yield deployments
        supervisors.proceed.set()


def test_a_service_with_a_deployment_under_way_takes_no_other(busy: Deployments) -> None:
    with pytest.raises(DeployError, match="a deployment of notes is under way"):
        busy.deploy("another-agent", "notes")
