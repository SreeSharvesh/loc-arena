"""The run's report after play of each tool a session was not offered, read from the gateway's log."""

from __future__ import annotations

import json
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.recorded_events import SERVICE_LOGS
from loc_arena.tool_offer import LACKED_TOOLS_FILE, name_tools, report_lacked_tools
from loc_arena.tools_gateway import TOOLS_GATEWAY_SERVICE

CONFIG = load_run_config("configs/aurora-efficiency.yaml")
AGENT = "serving-agent"
WHEN = "2026-11-15T12:36:24.403854Z"


def request_line(**fields: object) -> str:
    """One line of the gateway's log of a request, with ``fields`` beside its own."""
    return json.dumps({"level": "info", "time": WHEN, "scope": "request", "agent": AGENT, **fields})


def test_only_a_sessions_listing_that_lacks_a_named_tool_is_reported_whatever_a_call_records(
    tmp_path: Path,
) -> None:
    named = sorted(name_tools(CONFIG)[AGENT])
    (tmp_path / SERVICE_LOGS).mkdir()
    (tmp_path / SERVICE_LOGS / f"{TOOLS_GATEWAY_SERVICE}.log").write_text(
        "\n".join(
            [
                # a session's listing without list_notes
                request_line(
                    **{"mcp.session.id": "s1", "tools_listed": [t for t in named if t != "list_notes"]},
                ),
                # a check's listing outside any session, offered nothing
                request_line(tools_listed=[]),
                # a tool call whose arguments name the listing's key
                request_line(**{"mcp.session.id": "s1", "tool_args": {"labels": {"tools_listed": "x"}}}),
                '{"level":"info","time":"' + WHEN + '","tool_result":"a long result, split',
            ],
        ),
    )

    report_lacked_tools(CONFIG, tmp_path)

    lacked = [json.loads(line) for line in (tmp_path / LACKED_TOOLS_FILE).read_text().splitlines()]
    assert lacked == [
        {"time": "2026-11-15T12:36:24.403854+00:00", "agent": AGENT, "session": "s1", "tool": "list_notes"},
    ]
