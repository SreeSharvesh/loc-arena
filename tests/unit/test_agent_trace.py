from __future__ import annotations

from loc_arena.logging_.agent_trace import AgentTrace


def test_constructs_with_an_injected_wall_clock() -> None:
    AgentTrace(wall_clock=lambda: 1.0)
