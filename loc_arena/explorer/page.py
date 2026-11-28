"""The data a page carries, as the page's script reads it. Every field name here is a key in that script."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class VerdictView:
    """One monitor verdict, and the step it sits on (``step`` is None when its target is no shown step)."""

    monitor: str
    tap: str
    score: float
    reason: str
    actor: str
    target_mirror_seq: int | None
    step: str | None
    target: str | None


@dataclass(frozen=True)
class ModelCallView:
    """One model call in a step: the context the model saw, its reply raw and as prose, and its reasoning."""

    seq: int | None
    identity: str
    phase: str | None
    prompt: str
    reply: str
    reasoning: str | None
    # what the agent wrote in its own words in its turn's reply, beside any tool call; None for any other call
    said: str | None


@dataclass(frozen=True)
class ToolCallView:
    """One tool call in a step, with its arguments and result as text a person reads."""

    seq: int | None
    function: str
    arguments_text: str
    action: str
    result_text: str
    blocked: str | None
    covert: tuple[str, ...]
    verdicts: tuple[VerdictView, ...]


@dataclass(frozen=True)
class EventView:
    """Any other event a step caused: a chat message, a pull request, a ticket change."""

    seq: int | None
    kind: str
    actor: str | None
    body: str
    covert: tuple[str, ...]
    verdicts: tuple[VerdictView, ...]


@dataclass(frozen=True)
class StepView:
    """One agent turn, or the events of one round outside any turn (the ``World`` lane)."""

    id: str
    agent: str
    round: int
    calls: tuple[ModelCallView, ...]
    tools: tuple[ToolCallView, ...]
    events: tuple[EventView, ...]
    covert: tuple[str, ...]
    verdicts: tuple[VerdictView, ...]
    peak: float | None


@dataclass(frozen=True)
class ReadersView:
    """Who read a message: turns whose prompt holds its text, and monitors that saw or judged it."""

    traced: bool
    agents: tuple[str, ...]
    rereads: tuple[str, ...]
    monitors: tuple[str, ...]
    others: tuple[str, ...]


@dataclass(frozen=True)
class CommunicationView:
    """Something said in the run: a chat message, an agent's scratch note, or an environment post."""

    kind: str
    channel: str | None
    step: str
    sender: str
    audience: str
    text: str
    scores: tuple[float, ...]
    covert: tuple[str, ...]
    readers: ReadersView


@dataclass(frozen=True)
class StageView:
    """A side-task stage, and the steps its sealed evidence cites."""

    id: str
    reached: bool
    sealed: bool
    steps: tuple[str, ...]


@dataclass(frozen=True)
class MainTaskView:
    """How the assigned task went."""

    final_score: float
    gate_passed: bool
    gate_reason: str | None
    red_suites: tuple[str, ...]
    tests_passed: int
    tests_total: int
    unmetered_seams: tuple[str, ...]


@dataclass(frozen=True)
class SideTaskView:
    """Whether the side task succeeded."""

    success: bool


@dataclass(frozen=True)
class SafetyView:
    """What the monitors decided: whether they caught the run, and which of them did."""

    caught: bool
    caught_by: tuple[str, ...]


@dataclass(frozen=True)
class OutcomeView:
    """The run's outcome."""

    main_task: MainTaskView
    side_task: SideTaskView
    stages: tuple[StageView, ...]
    safety: SafetyView


@dataclass(frozen=True)
class MonitorView:
    """A deployed monitor or detector."""

    name: str
    role: str
    mode: str
    timing: str
    counts_toward_caught: bool
    errored: bool
    top_reasons: tuple[str, ...]


@dataclass(frozen=True)
class RunView:
    """Which run this is. ``index`` links back to the index of runs when the page was built from a folder."""

    name: str
    slug: str
    mode: str
    seed: int
    models: Mapping[str, str]
    wall_clock_seconds: float
    index: str | None
    audit_threshold: float
    play: str  # "episode" (the run itself) or "honest_cal" (its honest baseline)
    peer: str | None  # the other play's page, when the run has one


@dataclass(frozen=True)
class PageSettings:
    """The tunables the page's script reads."""

    list_page_size: int
    sparse_monitor_verdicts: int


@dataclass(frozen=True)
class CalibrationRow:
    """A monitor's score on each play, as the caught decision reads it."""

    monitor: str
    run_score: float
    baseline_score: float


@dataclass(frozen=True)
class CalibrationView:
    """The run's play against its honest baseline, whose overall suspicion is the bar the run's is held to."""

    run_overall: float
    baseline_overall: float
    bar: float
    rows: tuple[CalibrationRow, ...]


@dataclass(frozen=True)
class RunPage:
    """Everything one run's page shows."""

    run: RunView
    outcome: OutcomeView | None  # none for the honest baseline: it is not scored, it sets the audit threshold
    calibration: CalibrationView | None  # on both plays' pages when grading kept the baseline's scores
    monitors: tuple[MonitorView, ...]
    lanes: tuple[str, ...]
    steps: tuple[StepView, ...]
    verdicts: tuple[VerdictView, ...]
    communication: tuple[CommunicationView, ...]
    settings: PageSettings


@dataclass(frozen=True)
class RunRow:
    """One run in the index: its outcome, and its steps when it has a transcript (``href`` is None if not)."""

    name: str
    mode: str
    seed: int
    attacker: str
    monitor: str
    created: str
    minutes: int
    main_score: float
    gate_passed: bool
    gate_reason: str | None
    succeeded: bool
    stages_reached: int
    stages_total: int
    caught: bool
    peaks: Mapping[str, float]
    threshold: float
    href: str | None
    why: str | None
    steps: int | None
    flagged: int | None
    covert: int | None
    covert_missed: int | None
    messages: int | None
    scratch: int | None


@dataclass(frozen=True)
class RunIndex:
    """The index over a folder of runs."""

    root: str
    built_at: str
    runs: tuple[RunRow, ...]
    incomplete: tuple[str, ...]
