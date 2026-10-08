"""``played.json`` carries the per-agent traces from play to grading as JSON, which the grading host reads."""

from __future__ import annotations

from types import MappingProxyType

from loc_arena.harness import PlayedRun
from loc_arena.logging_.agent_trace import EpisodeTrace, ModelCall, TurnRecord, TurnRef

TURN = TurnRef("agent-main", 0)
TRACE = EpisodeTrace(
    turns=(TurnRecord(TURN, wall_start=1.25, wall_end=2.5),),
    sealed_lane=MappingProxyType({0: TURN, 1: None}),
    mirror_lane=MappingProxyType({0: TURN}),
    mirror_to_sealed=MappingProxyType({0: 0}),
    model_calls=(ModelCall("executing", "agent-main", "untrusted_agent", "prompt", "reply", 1, 2.0),),
    last_sealed_seq=1,
)


def test_a_played_run_written_as_json_reads_back_with_the_traces_it_was_given() -> None:
    played = PlayedRun(play_seconds=3.5, episode_trace=TRACE, calibration_trace=TRACE)

    read_back = PlayedRun.model_validate_json(played.model_dump_json())

    assert (read_back.play_seconds, read_back.episode_trace, read_back.calibration_trace) == (
        3.5,
        TRACE,
        TRACE,
    )
