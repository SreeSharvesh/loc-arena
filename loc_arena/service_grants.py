"""What a run config grants each agent on the scenario's live services, against what each service offers."""

from collections.abc import Iterable, Mapping, Sequence
from typing import Literal

from scenarios.loader import LiveService


def find_unknown_grant(
    kind: Literal["rights", "tools"],
    held: Mapping[str, Mapping[str, Sequence[str]]],
    live_services: Iterable[LiveService],
    scenario_name: str | None,
) -> str | None:
    """What is wrong when an agent ``held`` rights or tools a service does not offer, else ``None``.

    ``held`` maps each agent to its rights or tools by service. Rights or tools on a service with none, or
    outside its list, are refused: a typo must not pass silently.
    """
    offered = {service.name: getattr(service, kind) for service in live_services if getattr(service, kind)}
    for agent, by_service in held.items():
        for service, names in by_service.items():
            if service not in offered:
                return (
                    f"agent {agent!r} holds {kind} on {service!r}, which is no live service of scenario "
                    f"{scenario_name!r} with {kind}; those are: {sorted(offered)}"
                )
            if unknown := [name for name in names if name not in offered[service]]:
                return (
                    f"agent {agent!r} holds {kind} {unknown} on {service!r}, which has only "
                    f"{list(offered[service])}"
                )
    return None
