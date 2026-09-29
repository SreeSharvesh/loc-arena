"""What a run's ``scores.json`` records when the episode's wall-clock ceiling stopped its agents.

``episode.wall_clock_seconds`` bounds how long a model-policy episode's agents take turns
(``live.run_model_policy``); an episode it stops has turns its agents never took, so its scores cover a
shortened run. The harness writes ``WallClockCeilingStop`` under ``WALL_CLOCK_CEILING_KEY`` only for a run
the ceiling stopped, in its episode or its honest twin, so a run it did not stop writes what it always did.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from loc_arena.config import RunConfig
from loc_arena.metrics import GradedEpisode

WALL_CLOCK_CEILING_KEY: Final = "wall_clock_ceiling"


@dataclass(frozen=True)
class WallClockCeilingStop:
    """Which episodes of a run the wall-clock ceiling stopped, and that ceiling in seconds.

    ``seconds`` is ``episode.wall_clock_seconds``; ``calibration_stopped`` is ``None`` in honest mode, where
    the episode is its own calibration twin.
    """

    seconds: int
    episode_stopped: bool
    calibration_stopped: bool | None


def read_wall_clock_ceiling_stop(
    config: RunConfig,
    mode: str,
    episode: GradedEpisode,
    honest: GradedEpisode,
) -> WallClockCeilingStop | None:
    """The run's wall-clock ceiling stop, or ``None`` when the ceiling stopped none of its episodes."""
    calibration_stopped = honest.stopped_at_wall_clock_ceiling if mode == "attack" else None
    if not (episode.stopped_at_wall_clock_ceiling or calibration_stopped):
        return None
    return WallClockCeilingStop(
        seconds=config.episode.wall_clock_seconds,
        episode_stopped=episode.stopped_at_wall_clock_ceiling,
        calibration_stopped=calibration_stopped,
    )
