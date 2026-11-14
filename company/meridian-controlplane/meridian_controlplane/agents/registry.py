"""The registry of the agents Meridian runs: each agent's owner, its versions, and whether it is retired.

Every change to an agent's definition adds a version; earlier versions never change. A retired agent keeps
its versions and takes no new one. The registry keeps its agents, and each agent's identity with its
secrets, in a database (``meridian_common.records``), which it writes the agents in ``seed.yaml`` into when it
is new, so a registry that starts again on the same database holds what the last one held.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Annotated

import yaml
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from meridian_common.records import Database
from meridian_controlplane.agents.authority import AgentIdentity

SEEDED_AGENTS_FILE = Path(__file__).with_name("seed.yaml")
AgentName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]


class AgentDefinition(BaseModel):
    """What an agent runs: its model and its system prompt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    system_prompt: str


class AgentRegistration(AgentDefinition):
    """A new agent: its name and its first definition."""

    name: AgentName


class SeededAgent(AgentRegistration):
    """An agent the registry starts with, and the team that owns it."""

    owner: str = Field(min_length=1)


class AgentVersion(AgentDefinition):
    """One version of an agent's definition, numbered from 1, and who made it when."""

    number: int
    created_by: str
    created_at: datetime


class AgentSummary(BaseModel):
    """An agent as the API returns it: its owner, its identity, whether it is retired, its latest version."""

    name: str
    owner: str
    identity: str | None  # the principal its executions run as; none for a seeded agent
    retired: bool
    latest_version: AgentVersion


class _RegisteredAgent(BaseModel):
    model_config = ConfigDict(frozen=True)

    owner: str
    versions: list[AgentVersion]
    retired: bool = False


def _reveal_identity(identity: AgentIdentity) -> str:
    """``identity`` as JSON with its secrets in clear, which pydantic masks: for the database alone."""
    secrets = {service: secret.get_secret_value() for service, secret in identity.secrets.items()}
    badge = identity.badge.get_secret_value() if identity.badge else None
    return json.dumps({"principal": identity.principal, "secrets": secrets, "badge": badge})


def load_seeded_agents(seed_file: Path = SEEDED_AGENTS_FILE) -> list[SeededAgent]:
    """The agents listed in ``seed_file``."""
    return TypeAdapter(list[SeededAgent]).validate_python(yaml.safe_load(seed_file.read_text()))


class AgentRegistry:
    """The registered agents by name. A refused change raises ``HTTPException`` with the status to answer."""

    def __init__(self, seeded_agents: Iterable[SeededAgent], database: Database | None = None) -> None:
        """Hold what ``database`` holds, and ``seeded_agents``, each its owner's, if it is new.

        Without a database the registry is in memory.
        """
        self._database = database or Database()
        self._agents_by_name = self._database.records(
            "agents",
            _RegisteredAgent.model_validate_json,
            _RegisteredAgent.model_dump_json,
        )
        self._identities_by_name = self._database.records(
            "identities",
            AgentIdentity.model_validate_json,
            _reveal_identity,
        )
        self._registering = threading.Lock()  # a name stays free while its agent's identity is issued
        if not self._agents_by_name:
            with self._database.transaction():  # the whole seed, or a database that is still new
                for seeded_agent in seeded_agents:
                    if seeded_agent.name in self._agents_by_name:
                        raise ValueError(f"the seed names the agent {seeded_agent.name!r} twice")
                    self._add_agent(seeded_agent, seeded_agent.owner)

    def list_agents(self) -> list[AgentSummary]:
        """Every agent, retired ones included, by name."""
        return [self.get_agent(name) for name in sorted(self._agents_by_name)]

    def get_agent(self, name: str) -> AgentSummary:
        """The agent named ``name``; 404 if there is none."""
        agent, identity = self._find_agent(name), self._identities_by_name.get(name)
        return AgentSummary(
            name=name,
            owner=agent.owner,
            identity=identity.principal if identity else None,
            retired=agent.retired,
            latest_version=agent.versions[-1],
        )

    def list_versions(self, name: str) -> list[AgentVersion]:
        """Every version of the agent named ``name``, oldest first; 404 if there is none."""
        return list(self._find_agent(name).versions)

    def register(
        self,
        registration: AgentRegistration,
        registered_by: str,
        issue_identity: Callable[[], AgentIdentity | None],
    ) -> AgentSummary:
        """Register a new agent at version 1, owned by ``registered_by``; 409 if the name is taken.

        It gets the identity ``issue_identity`` gives once its name is known to be free.
        """
        with self._registering:
            if registration.name in self._agents_by_name:
                raise HTTPException(HTTPStatus.CONFLICT, f"agent {registration.name!r} already exists")
            identity = issue_identity()
            with self._database.transaction():  # the agent and what it runs as, together
                self._add_agent(registration, registered_by)
                if identity is not None:
                    self._identities_by_name[registration.name] = identity
        return self.get_agent(registration.name)

    def get_identity(self, name: str) -> AgentIdentity | None:
        """What the agent named ``name`` runs as; 404 if there is none."""
        self._find_agent(name)
        return self._identities_by_name.get(name)

    def add_version(self, name: str, definition: AgentDefinition, created_by: str) -> AgentSummary:
        """Give the agent ``definition`` as its next version; 404 if there is none, 409 if it is retired."""
        agent = self._find_agent_not_retired(name)
        version = _make_version(definition, number=len(agent.versions) + 1, created_by=created_by)
        self._agents_by_name[name] = agent.model_copy(update={"versions": [*agent.versions, version]})
        return self.get_agent(name)

    def retire(self, name: str) -> AgentSummary:
        """Retire the agent; 404 if there is none, 409 if it is already retired."""
        self._agents_by_name[name] = self._find_agent_not_retired(name).model_copy(update={"retired": True})
        return self.get_agent(name)

    def _add_agent(self, registration: AgentRegistration, registered_by: str) -> None:
        first_version = _make_version(registration, number=1, created_by=registered_by)
        self._agents_by_name[registration.name] = _RegisteredAgent(
            owner=registered_by,
            versions=[first_version],
        )

    def _find_agent(self, name: str) -> _RegisteredAgent:
        agent = self._agents_by_name.get(name)
        if agent is None:
            raise HTTPException(HTTPStatus.NOT_FOUND, f"no agent named {name!r}")
        return agent

    def _find_agent_not_retired(self, name: str) -> _RegisteredAgent:
        agent = self._find_agent(name)
        if agent.retired:
            raise HTTPException(HTTPStatus.CONFLICT, f"agent {name!r} is retired")
        return agent


def _make_version(definition: AgentDefinition, number: int, created_by: str) -> AgentVersion:
    return AgentVersion(
        model=definition.model,
        system_prompt=definition.system_prompt,
        number=number,
        created_by=created_by,
        created_at=datetime.now(UTC),
    )
