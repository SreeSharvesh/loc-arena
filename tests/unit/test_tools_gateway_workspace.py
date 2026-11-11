"""The tools gateway under ``harness: pi``: the workspace service is a target, called as the calling agent."""

from __future__ import annotations

from typing import Any

import yaml
from loc_arena.config import RunConfig, load_run_config
from loc_arena.tools_gateway import render_tools_gateway_config
from loc_arena.workspace_service import workspace_key

OWN_SESSIONS = load_run_config("configs/aurora-efficiency.yaml")
NATIVE = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def render_route(config: RunConfig) -> dict[str, Any]:
    tokens = {agent.id: f"token-of-{agent.id}" for agent in config.agents}
    document = yaml.safe_load(render_tools_gateway_config(config, tokens, {}))
    return document["binds"][0]["listeners"][0]["routes"][0]


def test_under_pi_each_agent_is_offered_the_workspace_tools_under_its_own_workspace_key() -> None:
    route = render_route(OWN_SESSIONS)

    workspace = next(
        target for target in route["backends"][0]["mcp"]["targets"] if target["name"] == "workspace"
    )
    sent = workspace["policies"]["transformations"]["request"]["set"]["authorization"]
    key = workspace_key("token-of-eval-agent")
    assert workspace["mcp"] == {"host": f"http://episode:{OWN_SESSIONS.settings.stack.workspace_port}/mcp"}
    assert f'"eval-agent": "{key}"' in sent
    assert "token-of-eval-agent" not in sent
    assert '"workspace": ["bash", "read_file"' in route["policies"]["mcpAuthorization"]["rules"][0]["allow"]


def test_the_native_loop_has_no_workspace_target() -> None:
    route = render_route(NATIVE)

    assert "workspace" not in [target["name"] for target in route["backends"][0]["mcp"]["targets"]]
