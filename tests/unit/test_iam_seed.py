"""What a scenario seeds into central IAM, and the document IAM is seeded from for one play."""

import dataclasses
import json
from pathlib import Path

import pytest
from loc_arena.config import ConfigError, load_run_config
from loc_arena.iam_bootstrap import managed_services, render_bootstrap
from loc_arena.iam_seed import IamSeed, add_iam_service
from loc_arena.settings import IamSettings
from scenarios.loader import load_scenario

PLAIN = load_run_config("configs/aurora-efficiency.deterministic.yaml")
MANAGED = ("notes", "agents", "forge", "chat", "jobsvc", "evalkit", "tickets", "modelstore")
ENABLED = IamSettings(enabled=True)
AGENTS = [agent.id for agent in PLAIN.agents]


def with_iam(tmp_path: Path):  # noqa: ANN201 - the deterministic fixture with IAM on, as aurora-efficiency.iam.yaml
    run = tmp_path / "iam.yaml"
    run.write_text("extends: aurora-efficiency.deterministic.yaml\niam:\n  enabled: true\n")
    return load_run_config(run, Path("configs"))


def test_iam_is_on_unless_the_run_config_turns_it_off() -> None:
    live = load_run_config("configs/aurora-efficiency.yaml")
    assert [service.name for service in live.live_services][-1] == "iam"
    assert managed_services(live) == MANAGED
    assert "iam" not in [service.name for service in PLAIN.live_services]
    assert managed_services(PLAIN) == ()


def test_agents_are_pointed_at_iams_own_description(tmp_path: Path) -> None:
    iam = next(service for service in with_iam(tmp_path).live_services if service.name == "iam")

    expected = "Central IAM: GET / says how to see your access and ask for more; approvers decide."
    assert iam.description == expected


def test_under_iam_every_agent_is_offered_its_inbox_and_iam_serves_only_that_tool(tmp_path: Path) -> None:
    config = with_iam(tmp_path)

    iam = next(service for service in config.live_services if service.name == "iam")

    assert iam.tools == ("access_inbox",)
    assert all(agent.sandbox.tools["iam"] == ("access_inbox",) for agent in config.agents)
    assert all("iam" not in agent.sandbox.tools for agent in PLAIN.agents)


def test_under_iam_a_listed_tool_iam_does_not_serve_is_still_refused_at_load(tmp_path: Path) -> None:
    run = tmp_path / "iam.yaml"
    run.write_text(
        "extends: aurora-efficiency.deterministic.yaml\niam:\n  enabled: true\n"
        "agents: [{id: typist, kind: k, trust: untrusted, branch: b, scope: {}, "
        "sandbox: {tools: {iam: [decide_access]}}}]\n",
    )

    with pytest.raises(ConfigError, match=r"agent 'typist' holds tools \['decide_access'\] on 'iam'"):
        load_run_config(run, Path("configs"))


def test_enabling_without_a_seed_or_with_a_wrong_one_is_refused() -> None:
    scenario = load_scenario("aurora_efficiency")
    bare = dataclasses.replace(scenario, meta={k: v for k, v in scenario.meta.items() if k != "iam"})
    with pytest.raises(ValueError, match="iam: block"):
        add_iam_service(scenario.live_services, bare, ENABLED, AGENTS)
    ghost = {"resources": {"ghost": dict(scenario.meta["iam"]["resources"]["notes"])}}
    wrong = dataclasses.replace(scenario, meta={**scenario.meta, "iam": ghost})
    with pytest.raises(ValueError, match="does not run"):
        add_iam_service(scenario.live_services, wrong, ENABLED, AGENTS)
    assert IamSeed.model_validate(scenario.meta["iam"]).resources["notes"].owner_team == "research-platform"


def test_a_service_agents_reach_that_iam_does_not_manage_is_refused() -> None:
    scenario = load_scenario("aurora_efficiency")
    resources = {name: seed for name, seed in scenario.meta["iam"]["resources"].items() if name != "forge"}
    without = dataclasses.replace(scenario, meta={**scenario.meta, "iam": {"resources": resources}})
    with pytest.raises(ValueError, match=r"outside central IAM; give each an iam: resource: \['forge'\]"):
        add_iam_service(scenario.live_services, without, ENABLED, AGENTS)


@pytest.mark.parametrize(
    ("approver", "complaint"),
    [
        (None, "approver"),  # every service needs one
        ({"principal": "agent-main", "approves": ["read"]}, "'agent-main', an agent under test"),
        (
            {"principal": "notes-owner", "approves": ["grant"]},
            r"approves permissions nobody can request: \['grant'\]",
        ),
        ({"principal": "chat-service", "approves": ["read"]}, "as a service's own principal is"),
    ],
    ids=["no approver", "an agent under test", "a permission nobody can request", "a service's own name"],
)
def test_a_service_whose_approver_is_missing_or_wrong_is_refused(
    approver: dict[str, object] | None,
    complaint: str,
) -> None:
    scenario = load_scenario("aurora_efficiency")
    notes = {
        key: value for key, value in scenario.meta["iam"]["resources"]["notes"].items() if key != "approver"
    }
    resources = {
        **scenario.meta["iam"]["resources"],
        "notes": notes | ({"approver": approver} if approver else {}),
    }
    seeded = dataclasses.replace(scenario, meta={**scenario.meta, "iam": {"resources": resources}})

    with pytest.raises(ValueError, match=complaint):
        add_iam_service(scenario.live_services, seeded, ENABLED, AGENTS)


def test_the_bootstrap_gives_each_agent_its_configured_rights_and_tools_and_names_its_approvers(
    tmp_path: Path,
) -> None:
    document = json.loads(render_bootstrap(with_iam(tmp_path)))
    # Expected values are read off configs/aurora-efficiency.yaml's sandbox blocks, not computed from them.
    assert document["grants"]["notes"]["agent-main"] == ["read", "write"]
    assert "controlplane-agent" not in document["grants"]["notes"]  # it holds no notes right
    assert document["grants"]["forge"]["datapipe-agent"] == ["open_pr"]
    assert document["grants"]["chat"]["agent-main"] == ["read", "write"]
    # Each service's approver, as scenarios/aurora_efficiency/scenario.yaml names it, and no agent.
    assert document["resources"]["notes"]["approver"] == {
        "principal": "notes-owner",
        "approves": ["read", "write"],
    }
    assert document["resources"]["agents"]["approver"] == {"principal": "agents-owner", "approves": ["read"]}
    assert document["operators"] == []  # the Aurora scenario names none


@pytest.mark.parametrize(
    "block",
    [
        'enabled: "true"',  # a quoted boolean is a mistake, not true
        "enabled: 1",
        'port: "8000"',
        "port: 0",
        "port: 65536",  # a port nothing can bind
        "port: true",
        "retries: 1.5",
        "retries: -1",
        'timeout_seconds: "2"',
        "timeout_seconds: 0",
        "renewal_grace_seconds: -1",
        "service: Not_A_Host",
        'service: ""',
    ],
)
def test_the_iam_settings_are_strict_scalars_with_bounds(tmp_path: Path, block: str) -> None:
    run = tmp_path / "bad.yaml"
    run.write_text(f"extends: aurora-efficiency.deterministic.yaml\niam:\n  {block}\n")
    with pytest.raises(ConfigError):
        load_run_config(run, Path("configs"))


def test_valid_iam_settings_are_accepted_including_a_whole_number_where_a_float_is_meant(
    tmp_path: Path,
) -> None:
    run = tmp_path / "ok.yaml"
    run.write_text(
        "extends: aurora-efficiency.deterministic.yaml\niam:\n  enabled: true\n  port: 65535\n"
        "  timeout_seconds: 2\n",
    )
    settings = load_run_config(run, Path("configs")).settings.iam
    assert (settings.enabled, settings.port, settings.timeout_seconds) == (True, 65535, 2.0)


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("this is not json", "not a valid manifest"),
        ("[]", "not a valid manifest"),
        ('{"resource": "notes"}', "not a valid manifest"),  # no operations
        ('{"resource": "notes", "operations": {}}', "not a valid manifest"),
        ('{"resource": "wiki", "operations": {"a": {"permission": "read"}}}', "declares resource 'wiki'"),
        (
            '{"resource": "notes", "operations": {"a": {"permission": "read"}}, "tools": {"t": "missing"}}',
            "does not declare",
        ),
    ],
)
def test_a_seed_whose_manifest_cannot_be_rendered_is_refused_when_the_config_loads(
    tmp_path: Path,
    text: str,
    complaint: str,
) -> None:
    scenario = load_scenario("aurora_efficiency")
    bad = tmp_path / "manifest.json"
    bad.write_text(text)
    notes = {
        **scenario.meta["iam"]["resources"]["notes"],
        "manifest": str(bad),
    }  # an absolute path is used as it is
    meta = {**scenario.meta, "iam": {"resources": {"notes": notes}}}
    with pytest.raises(ValueError, match=complaint):
        add_iam_service(scenario.live_services, dataclasses.replace(scenario, meta=meta), ENABLED, AGENTS)


def test_a_service_declaring_a_right_its_manifest_never_checks_is_refused() -> None:
    scenario = load_scenario("aurora_efficiency")
    services = tuple(
        dataclasses.replace(service, rights=(*service.rights, "admin"))
        if service.name == "notes"
        else service
        for service in scenario.live_services
    )

    with pytest.raises(ValueError, match=r"declares rights its iam manifest never checks: \['admin'\]"):
        add_iam_service(services, scenario, ENABLED, AGENTS)


def test_an_approver_holding_two_roles_in_one_owner_team_is_refused() -> None:
    scenario = load_scenario("aurora_efficiency")
    resources = dict(scenario.meta["iam"]["resources"])
    agents_approver = resources["agents"]["approver"]
    resources["notes"] = {**resources["notes"], "approver_role": "lead", "approver": agents_approver}
    seeded = dataclasses.replace(scenario, meta={**scenario.meta, "iam": {"resources": resources}})

    with pytest.raises(ValueError, match="approver 'agents-owner' holds two roles in one owner team"):
        add_iam_service(scenario.live_services, seeded, ENABLED, AGENTS)


@pytest.mark.parametrize(
    ("service", "delegation", "complaint"),
    [
        ("jobsvc", {"wiki": ["read"]}, "delegates rights on 'wiki', which the iam seed does not manage"),
        ("jobsvc", {"notes": ["admin"]}, r"manifest of 'notes' never checks: \['admin'\]"),
        (
            "notes",
            {"notes": ["read"]},
            "'notes' delegates rights, but its iam manifest marks no operation launch",
        ),
    ],
    ids=["a service the seed does not manage", "a permission it never checks", "a service launching nothing"],
)
def test_a_delegation_iam_would_refuse_to_seed_is_refused_when_the_config_loads(
    service: str,
    delegation: dict[str, list[str]],
    complaint: str,
) -> None:
    scenario = load_scenario("aurora_efficiency")
    resources = dict(scenario.meta["iam"]["resources"])
    resources[service] = {**resources[service], "delegation": delegation}
    seeded = dataclasses.replace(scenario, meta={**scenario.meta, "iam": {"resources": resources}})

    with pytest.raises(ValueError, match=complaint):
        add_iam_service(scenario.live_services, seeded, ENABLED, AGENTS)


@pytest.mark.parametrize(
    ("holders", "complaint"),
    [
        (
            {"serving-agent": ["operate"]},
            "'serving-agent' holds rights on 'jobsvc' from the seed, but is an agent",
        ),
        (
            {"notes-service": ["operate"]},
            "'notes-service' holds rights on 'jobsvc' from the seed, but is an agent",
        ),
        ({"releng": ["deploy"]}, r"manifest of 'jobsvc' never checks: \['deploy'\]"),
    ],
    ids=["an agent of the run", "a service's own principal", "a permission it never checks"],
)
def test_a_company_principal_seeded_holding_what_iam_would_refuse_is_refused_when_the_config_loads(
    holders: dict[str, list[str]],
    complaint: str,
) -> None:
    scenario = load_scenario("aurora_efficiency")
    resources = dict(scenario.meta["iam"]["resources"])
    resources["jobsvc"] = {**resources["jobsvc"], "holders": holders}
    seeded = dataclasses.replace(scenario, meta={**scenario.meta, "iam": {"resources": resources}})

    with pytest.raises(ValueError, match=complaint):
        add_iam_service(scenario.live_services, seeded, ENABLED, AGENTS)
