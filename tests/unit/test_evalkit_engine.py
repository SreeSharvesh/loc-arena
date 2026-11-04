"""The eval service in a stack run: the gateway serves every model it seeds, and its records are events."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.evalkit_service import main
from loc_arena.recorded_events import _record_line_parser

CONFIGS = Path(__file__).parents[2] / "configs"
CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
EVALKIT = next(service for service in CONFIG.live_services if service.name == "evalkit")
AGENTS = tuple(agent.id for agent in CONFIG.agents)


def test_a_service_with_tools_and_rights_records_tool_calls_and_requests_both() -> None:
    parse = _record_line_parser(EVALKIT, CONFIG)
    tool_call = {
        "time": "2026-09-01T12:00:00+00:00",
        "gen_ai.tool.name": "start_run",
        "gen_ai.tool.call.arguments": {"eval_id": "arithmetic", "model_id": "aurora-7b-v3"},
        "gen_ai.tool.call.result": {"run_id": "run-0439"},
        "gen_ai.agent.name": "agent-main",
    }
    request = {
        "wall_ts": "2026-09-01T12:00:01+00:00",
        "caller": "agent-main",
        "method": "POST",
        "path": "/runs/run-0439/publish",
        "status": 200,
        "operation": "publish_run",
        "arguments": {"run_id": "run-0439"},
    }
    read = {**request, "method": "GET", "path": "/runs/run-0439", "operation": "get_run"}

    parsed = [parse(json.dumps(line), AGENTS) for line in (tool_call, request, read)]

    assert [[(e.kind, e.tool, e.actor_uid, e.target_id) for e in p.events] if p else p for p in parsed] == [
        [("action", "start_run", "agent-main", "arithmetic")],
        [("action", "publish_run", "agent-main", "run-0439")],
        [],
    ]


def test_the_service_will_not_start_on_a_seeded_model_the_gateway_does_not_serve(tmp_path: Path) -> None:
    run_config = shutil.copytree(CONFIGS, tmp_path / "configs") / "run.yaml"
    run_config.write_text(
        "extends: aurora-efficiency.yaml\n"
        "gateway: {runner_model_aliases: {aurora-7b-v3: deepseek/deepseek-v4.1-flash}}\n",
    )

    with pytest.raises(ValueError, match="aurora-13b-v2, aurora-7b-v4-rc1"):
        main([str(run_config), "evalkit"])
