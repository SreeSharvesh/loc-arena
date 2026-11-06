"""A hyperparameter sweep trial: its config and the learning curve, one training step at a time.

A trial's quality is a validation metric that rises with diminishing returns toward a ceiling (``target``) its
hyperparameters fix; ``rate`` is how fast it converges there. :func:`advance` runs ONE training step and is
the unit of sweep cost (one step of one trial is one job-step), and a trial's metric is only known after the
steps that produced it, so a trial cannot be judged without being advanced. A high-``target`` trial that
converges slowly is behind a low-``target`` trial that converges fast for the first steps and only overtakes
later, so an early-stopping rule that prunes on the first step or two drops the eventual winner.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_evalkit.errors import SweepError

CURVE_SCALE = 1000  # the denominator of ``rate``: the per-step fraction of the remaining gap a trial closes


@dataclass(frozen=True)
class TrialConfig:
    """One trial's hyperparameters: the metric ceiling it converges toward and how fast it gets there."""

    trial_id: str
    target: int  # the metric ceiling the trial's learning curve approaches
    rate: int  # per-step fraction of the remaining gap closed, over CURVE_SCALE (1..CURVE_SCALE)

    def __post_init__(self) -> None:
        """Reject a negative ceiling or a rate outside ``1..CURVE_SCALE``."""
        if self.target < 0:
            raise SweepError(f"trial {self.trial_id!r}: target must be >= 0", code="evalkit.sweep")
        if not 1 <= self.rate <= CURVE_SCALE:
            raise SweepError(
                f"trial {self.trial_id!r}: rate must be 1..{CURVE_SCALE}, got {self.rate}",
                code="evalkit.sweep",
            )


@dataclass(frozen=True)
class TrialState:
    """A trial's progress: the step it has reached and the metric value measured there."""

    trial_id: str
    step: int
    value: int


def initial_state(config: TrialConfig) -> TrialState:
    """The trial before any step has run: step 0, metric 0."""
    return TrialState(trial_id=config.trial_id, step=0, value=0)


def advance(state: TrialState, config: TrialConfig) -> TrialState:
    """Run one training step: close part of the gap to ``target`` (at least one) until it is saturated.

    The unit of sweep cost. Deterministic and monotone: the metric rises toward ``target`` by a ``rate``
    fraction of the remaining gap (at least one while short of it), so trials separate over steps and the
    ranking by metric settles only after several steps, not on the first.
    """
    if config.trial_id != state.trial_id:
        raise SweepError(
            f"state is trial {state.trial_id!r} but config is {config.trial_id!r}",
            code="evalkit.sweep",
        )
    gap = config.target - state.value
    gain = 0 if gap <= 0 else max(1, gap * config.rate // CURVE_SCALE)
    return TrialState(trial_id=state.trial_id, step=state.step + 1, value=state.value + gain)


def running_metric(state: TrialState) -> int:
    """The trial's metric as last measured (higher is better)."""
    return state.value
