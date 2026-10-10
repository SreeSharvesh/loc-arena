"""The host variables compose reads the agents' identities on live services from."""

from collections import Counter
from collections.abc import Iterable

from scenarios.loader import LiveService


def holds_identities(service: LiveService) -> bool:
    """Whether each agent gets its own identity on ``service``: it has rights or serves tools."""
    return bool(service.rights or service.tools)


def identity_variable(service: str, agent_id: str) -> str:
    """Where compose reads an agent's identity on a live service from, on the host."""
    return f"LOC_ARENA_IDENTITY_{service}_{agent_id}".upper().replace("-", "_")


def find_shared_identity_variable(
    agent_ids: Iterable[str],
    live_services: Iterable[LiveService],
) -> str | None:
    """What is wrong when two identities would share one host variable, and so one value, else ``None``.

    An agent could then pass as another: a service ``notes`` with agent ``agent-main``, and a service
    ``notes-agent`` with agent ``main``, both read ``LOC_ARENA_IDENTITY_NOTES_AGENT_MAIN``.
    """
    agents = list(agent_ids)
    pairs = [
        (service.name, agent) for service in live_services if holds_identities(service) for agent in agents
    ]
    variables = Counter(identity_variable(*pair) for pair in pairs)
    shared = [pair for pair in pairs if variables[identity_variable(*pair)] > 1]
    return (
        f"the identities of (service, agent) pairs {shared} would share one host variable" if shared else None
    )
