"""The company's telemetry in a rendered compose file: who is on its network, and who holds its secrets."""

from __future__ import annotations

import dataclasses

from loc_arena.config import load_run_config
from loc_arena.episode_stack import REPOSITORY, issue_secrets, render_compose

RUN = "aurora-efficiency"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
TELEMETRY_SECRETS = {"telemetry_store_users", "telemetry_collector_config"}


def test_the_store_and_the_companys_collector_alone_join_its_network_and_hold_its_secrets() -> None:
    compose = render_compose(CONFIG, REPOSITORY, RUN, [])

    services = compose["services"]
    joined = {name: service["networks"] for name, service in services.items() if "telemetry" in name}
    holders = {
        name
        for name, service in services.items()
        for secret in service.get("secrets", [])
        if (secret if isinstance(secret, str) else secret["source"]) in TELEMETRY_SECRETS
    }
    assert joined == {"telemetry": ["telemetry-net"], "telemetry-collector": ["agent-net", "telemetry-net"]}
    assert [name for name, service in services.items() if "telemetry-net" in service["networks"]] == [*joined]
    assert compose["networks"]["telemetry-net"] == {"internal": True}
    assert holders == {"telemetry", "telemetry-collector"}
    assert services["episode"]["depends_on"]["telemetry-collector"] == {"condition": "service_started"}


def test_a_scenario_with_no_telemetry_store_has_no_collector_and_no_secret_of_its() -> None:
    kept = tuple(service for service in CONFIG.live_services if not service.collects)
    config = dataclasses.replace(CONFIG, live_services=kept)

    compose = render_compose(config, REPOSITORY, RUN, [])
    attack, _ = issue_secrets(config)

    assert not [name for name in (*compose["services"], *compose["networks"]) if "telemetry" in name]
    assert not [name for name in (*compose["secrets"], *attack) if "telemetry" in name.lower()]
