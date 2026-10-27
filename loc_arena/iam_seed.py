"""What a scenario seeds into central IAM: which live services are managed, and who owns each.

A pack's ``iam:`` block names, per live service, the manifest the service ships, the owner policy its
requests follow and the approver that decides them: a principal of the company, never an agent under test,
with the permissions it approves (it denies the rest); the company principals that hold permissions on it
from the start (``holders``), such as its operators; and, for a service that launches work, its
``delegation``: what that work may be given. Each agent's starting rights on a service, from the run
config, become its baseline grants, and may be any permission the service's manifest checks. Its tools are
only what the tools gateway offers it: calling one takes the permission its operation checks, held or asked
for.
The block is inert only where the run config turns ``iam.enabled`` off (an in-process run). Every service an
agent reaches, by tools or by rights, must be one of its resources.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr
from scenarios.loader import EngineModule, LiveService, Scenario

from loc_arena.settings import IamSettings

if TYPE_CHECKING:  # the config module imports this one, so the type is for annotations only
    from loc_arena.config import AgentConfig

IAM_RIGHT = "request"  # IAM declares one right only so the stack issues every agent an identity on it
INBOX_TOOL = "access_inbox"  # the one tool IAM serves, through the tools gateway: each agent's own inbox
REPOSITORY = (
    Path(__file__).resolve().parents[1]
)  # manifest paths are relative to it; the engine image mirrors it
CompanyPrincipal = Annotated[StrictStr, Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")]


class ApproverSeed(BaseModel):
    """The principal that decides a service's access requests, and what it approves when asked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: CompanyPrincipal = Field(
        description="Its principal in IAM: a member of the owner team, and never an agent of the run.",
    )
    approves: tuple[StrictStr, ...] = Field(
        description="The requestable permissions it approves; it denies a request for any other.",
    )


class ResourceSeed(BaseModel):
    """One managed service's manifest, owner policy and approver."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: StrictStr = Field(description="The service's manifest, relative to the repository root.")
    owner_team: Annotated[StrictStr, Field(min_length=1)]
    approver_role: Annotated[StrictStr, Field(min_length=1)]
    approver: ApproverSeed
    max_seconds: Annotated[StrictInt, Field(gt=0)] = Field(
        description="The longest finite grant a request may ask.",
    )
    allow_ongoing: StrictBool = False
    max_pending: Annotated[StrictInt, Field(gt=0)] = 20
    records_actions: StrictBool = Field(
        default=False,
        description="Whether the service writes its own operation record of each call it forwards to IAM, "
        "which makes IAM's event for it a duplicate; a service that does not leaves IAM's event as the only "
        "record.",
    )
    delegation: dict[StrictStr, tuple[StrictStr, ...]] = Field(
        default_factory=dict,
        description="What the work the service launches may be given, by service: each piece of work gets "
        "its own identity holding those of these permissions its sponsor holds. Needs a manifest operation "
        "marked launch.",
    )
    holders: dict[CompanyPrincipal, tuple[StrictStr, ...]] = Field(
        default_factory=dict,
        description="Principals of the company that hold permissions on the service from the start, such as "
        "its operators' that nobody may request; never an agent of the run.",
    )


class IamSeed(BaseModel):
    """The pack's ``iam:`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resources: dict[StrictStr, ResourceSeed]
    operators: tuple[StrictStr, ...] = Field(
        default=(),
        description="Agents who may disable a principal through IAM. None unless the scenario names them.",
    )


def load_seed(scenario: Scenario | None) -> IamSeed | None:
    """The scenario's IAM seed, or ``None`` when it declares none."""
    declared = None if scenario is None else scenario.meta.get("iam")
    return None if declared is None else IamSeed.model_validate(declared)


def find_seed_problem(  # noqa: C901 - predates the complexity gate
    seed: IamSeed,
    live_services: tuple[LiveService, ...],
    agents: Collection[str],
) -> str | None:
    """What is wrong with a seed against the scenario's services and the run's ``agents``, else ``None``."""
    live = {service.name: service for service in live_services}
    if unknown := sorted(set(seed.resources) - set(live)):
        return f"the iam seed names services the scenario does not run: {unknown}"
    roles: dict[str, tuple[str, str]] = {}
    heads: dict[str, ManifestHead] = {}
    for name, resource in seed.resources.items():
        head = _read_manifest_head(name, resource.manifest)
        if isinstance(head, str):
            return head
        if problem := _manifest_problem(name, head, live[name].rights, resource.approver):
            return problem
        if problem := _holders_problem(
            name,
            resource.holders,
            head,
            {*agents, *map(_service_principal, seed.resources)},
        ):
            return problem
        heads[name] = head
        principal, role = resource.approver.principal, (resource.owner_team, resource.approver_role)
        if principal in agents:
            return f"the approver of {name!r} is {principal!r}, an agent under test"
        if principal in map(_service_principal, seed.resources):  # IAM names each service's own so
            return f"the approver of {name!r} is named {principal!r}, as a service's own principal is"
        if (
            roles.setdefault(principal, role) != role
        ):  # one membership per team: a second would replace the first
            return f"approver {principal!r} holds two roles in one owner team: {roles[principal]} and {role}"
    for name, resource in seed.resources.items():
        if problem := _delegation_problem(name, resource.delegation, heads):
            return problem
    reached = {service.name for service in live_services if service.tools or service.rights}
    if unmanaged := sorted(reached - set(seed.resources)):
        return f"agents reach services outside central IAM; give each an iam: resource: {unmanaged}"
    return None


class ManifestHead(BaseModel):
    """What the host reads of a manifest to check a seed and a run config; the full check is the service's.

    The engine cannot import ``meridian_iam`` (the episode container has no company packages), so it parses
    the JSON into this typed shape at the boundary and reports a manifest it could not read before any
    bootstrap.
    """

    model_config = ConfigDict(extra="ignore")

    resource: StrictStr
    operations: dict[StrictStr, dict[StrictStr, object]] = Field(min_length=1)
    tools: dict[StrictStr, StrictStr] = Field(default_factory=dict)

    def checked(self) -> set[str]:
        """The permissions some operation checks."""
        return {
            permission
            for spec in self.operations.values()
            if isinstance(permission := spec.get("permission"), str)
        }


def read_manifests(scenario: Scenario | None) -> dict[str, ManifestHead]:
    """The manifest of each service the scenario's IAM seed manages, by service; none without a seed.

    Raises ``ValueError`` for a manifest that is missing, malformed or another service's.
    """
    seed = load_seed(scenario)
    heads = (
        {}
        if seed is None
        else {name: _read_manifest_head(name, r.manifest) for name, r in seed.resources.items()}
    )
    if problems := [head for head in heads.values() if isinstance(head, str)]:
        raise ValueError(problems[0])
    return {name: head for name, head in heads.items() if isinstance(head, ManifestHead)}


def starting_rights(
    live_services: tuple[LiveService, ...],
    manifests: Mapping[str, ManifestHead],
) -> dict[str, tuple[str, ...]]:
    """What an agent may start with on each live service: what its manifest checks, else its own rights."""
    return {
        service.name: tuple(sorted(manifests[service.name].checked()))
        if service.name in manifests
        else service.rights
        for service in live_services
    }


def _read_manifest_head(name: str, manifest: str) -> ManifestHead | str:
    """The manifest a seed names for ``name``, or what is wrong with it: missing, malformed, another's."""
    path = REPOSITORY / manifest
    if not path.is_file():
        return f"the iam seed for {name!r} names no manifest file: {manifest}"
    try:
        head = ManifestHead.model_validate_json(path.read_text())
    except (OSError, ValueError) as error:
        return f"the iam manifest of {name!r} ({path.name}) is not a valid manifest: {error}"
    if head.resource != name:
        return f"the iam manifest of {name!r} declares resource {head.resource!r}"
    return head


def _manifest_problem(
    name: str,
    head: ManifestHead,
    rights: Collection[str],
    approver: ApproverSeed,
) -> str | None:
    """What is wrong with the manifest a seed names, else ``None``.

    Binding tools to operations it does not declare; never checking one of the service's ``rights`` (a right
    no operation checks could be granted and never enforced); or not letting anyone request what the approver
    approves.
    """
    if unknown := sorted(set(head.tools.values()) - set(head.operations)):
        return f"the iam manifest of {name!r} binds tools to operations it does not declare: {unknown}"
    if unchecked := sorted(set(rights) - head.checked()):
        return f"the service {name!r} declares rights its iam manifest never checks: {unchecked}"
    requestable = {spec.get("permission") for spec in head.operations.values() if spec.get("requestable")}
    if unrequestable := sorted(set(approver.approves) - requestable):
        return f"the approver of {name!r} approves permissions nobody can request: {unrequestable}"
    return None


def _holders_problem(
    name: str,
    holders: Mapping[str, Collection[str]],
    head: ManifestHead,
    taken: Collection[str],
) -> str | None:
    """What is wrong with the company principals seeded holding permissions on ``name``, else ``None``.

    A holder is never one of the ``taken`` principals (an agent of the run, a service's own), and holds only
    permissions the service's manifest checks.
    """
    for holder, permissions in holders.items():
        if holder in taken:
            return (
                f"{holder!r} holds rights on {name!r} from the seed, but is an agent of the run or a service"
            )
        if unchecked := sorted(set(permissions) - head.checked()):
            return f"{holder!r} holds permissions the iam manifest of {name!r} never checks: {unchecked}"
    return None


def _service_principal(service: str) -> str:
    """The principal IAM names a managed service's own."""
    return f"{service}-service"


def _delegation_problem(
    name: str,
    delegation: Mapping[str, Collection[str]],
    heads: Mapping[str, ManifestHead],
) -> str | None:
    """What is wrong with what ``name`` may hand the work it launches, else ``None``.

    Its manifest marks no operation that launches work, or it delegates on a service the seed does not manage
    or a permission that service never checks; IAM would refuse to seed either.
    """
    if delegation and not any(spec.get("launch") is True for spec in heads[name].operations.values()):
        return f"{name!r} delegates rights, but its iam manifest marks no operation launch"
    for target, permissions in delegation.items():
        if target not in heads:
            return f"{name!r} delegates rights on {target!r}, which the iam seed does not manage"
        if unchecked := sorted(set(permissions) - heads[target].checked()):
            return f"{name!r} delegates permissions the iam manifest of {target!r} never checks: {unchecked}"
    return None


def add_iam_service(
    live_services: tuple[LiveService, ...],
    scenario: Scenario | None,
    settings: IamSettings,
    agents: Collection[str],
) -> tuple[LiveService, ...]:
    """``live_services`` plus central IAM as one more of them, when the run config enables it.

    Raises ``ValueError`` when the scenario seeds nothing for IAM to manage, or the seed is wrong.
    """
    if not settings.enabled:
        return live_services
    seed = load_seed(scenario)
    if seed is None:
        raise ValueError("iam.enabled needs the scenario pack to declare an iam: block")
    if problem := find_seed_problem(seed, live_services, agents):
        raise ValueError(problem)
    if any(service.name == settings.service for service in live_services):
        raise ValueError(f"the scenario already runs a service named {settings.service!r}")
    iam = LiveService(
        name=settings.service,
        source=EngineModule("loc_arena.iam_service"),
        port=settings.port,
        command=None,
        healthcheck=None,
        rights=(IAM_RIGHT,),
        description="Central IAM: GET / says how to see your access and ask for more; approvers decide.",
        tools=(INBOX_TOOL,),
    )
    return (*live_services, iam)


def offer_inbox(agents: tuple[AgentConfig, ...], settings: IamSettings) -> tuple[AgentConfig, ...]:
    """``agents``, each also offered IAM's ``access_inbox`` when the run config enables IAM.

    Every agent has an inbox: the tools gateway delivers it at each turn's start with the agent's inbox key,
    which reads that inbox and nothing else. Any IAM tools the config lists are kept, so the config's own
    validation still sees them. Without IAM the agents are returned as they are.
    """
    if not settings.enabled:
        return agents

    def offered(tools: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        listed = tools.get(settings.service, ())
        return {**tools, settings.service: listed if INBOX_TOOL in listed else (*listed, INBOX_TOOL)}

    return tuple(
        dataclasses.replace(
            agent,
            sandbox=dataclasses.replace(agent.sandbox, tools=offered(agent.sandbox.tools)),
        )
        for agent in agents
    )
