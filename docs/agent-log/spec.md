# Per-agent Inspect logging: spec

## Goal

Every run writes a real Inspect `.eval` log with one span per agent, and renders it as a transcript with one
lane per agent. Each lane shows what the agent saw, what it said, what it did, and every sealed event its
turns caused. With `logging.agent_transcript` off, the `.eval` is a JSON placeholder and no transcript is written.

## Why attribution works

An episode runs one agent turn at a time on one thread (`_drive_team` in `live.py`). Every event appended while a
turn is bound was caused by that turn. Every event appended with no turn bound belongs to the World lane (NPCs,
ticks, closes).

## Invariants

| # | Invariant | Reason |
|---|---|---|
| I1 | The trace never writes to the sealed or mirror log, so their bytes are the same with the flag on or off | the verifier, the monitors and the byte-identical repro test read them |
| I2 | The trace is held in memory until both episodes have played, then written only to `played.json` (for grading) and the bundle | agent code can read the run directory while an episode is live |
| I3 | Every sealed seq up to the episode boundary maps to exactly one lane; the exporter raises otherwise | `AppendOnlyLog.on_append` swallows subscriber exceptions |
| I4 | Sealed events after the boundary are post-episode: every post-boundary event goes to the lane `(actor_uid, turn)` when the trace has that turn, else World | monitor records and the events built from service records (`recorded_events.py`) are appended after play by writers with no subscriber |

## Lane rule

A seq's lane is the turn bound when it was appended, else World. Never `actor_uid` inside the boundary: in attack
mode the world activity writes a scripted PR stamped `agent-main` outside any turn. After the boundary,
`(actor_uid, turn)` names the lane when it is a native turn of the trace: the record builder gives an event the
turn of its agent that covered the record's wall time, else turn 0, and notes the record in
`unattributed_records.jsonl`.

## The trace (`loc_arena/logging_/agent_trace.py`)

`open_episode_logs` opens an episode's two logs and, when traced, an `AgentTrace` subscribed to both;
`live.play_model_episode` uses it for every policy. `Agent.run_turn` binds each turn with `trace.turn` (phase
`deciding`, then `executing` before the tool layer); turns never nest, and binding one while another is bound
raises. Each append to either log is tagged with the bound turn or World, and each mirror event is paired with its
sealed twin, since the two logs number independently. The episode's `GatewayCore` reports each provider call, with
the post-injection prompt, after writing its sealed record. `finish(last_sealed_seq)` returns a read-only
`EpisodeTrace` whose last sealed seq is the boundary for I3 and I4.

## The Inspect log (`loc_arena/logging_/inspect_export.py`)

One `.eval` per run, one sample per episode: `episode` (the graded run, the only one with scores) and, in attack
mode, `honest_cal`. Each sample has a root `episode` span, one `agent` span per agent, and one `turn` span per turn
that wrote a sealed event, holding that turn's events and those built for it after play. A sealed
`inference_call` with a recorded model call is a `ModelEvent`, a sealed `action` a `ToolEvent` (an error when
blocked), and every other sealed event an `InfoEvent`. Each event carries its sealed `seq` and its mirror twin's
`mirror_seq`, because monitor verdicts name mirror seqs. World events sit in the root span. A sealed seq inside
the boundary with no lane raises `UnassignedEventError`; so does rerunning into an existing run directory.

## Reasoning (`loc_arena/logging_/reasoning.py`)

A stack run's gateway records every reply as OpenRouter returned it, reasoning included, in a call log two
folders above the run's bundle (`locate_call_log`); the sealed log keeps only each reply's fingerprint.
`read_reasoning` gives the reasoning by the sealed seq of its call. A recorded reply belongs to the sealed
`inference_call` whose `output_fp` it fingerprints to, as the gateway computes it: the assistant message, or the
bare text for a `prompt` or batch call. Calls with the same reply pair in order, each sealed event with the first
recorded call not yet paired. The call log covers the honest twin too, which played after the run, so given the
twin's folder it pairs the run's calls first and returns the twin's. A refused, failed or cut-off call pairs with
nothing, and a run with no call log where a stack run leaves it (in process, or a bundle moved away) has none.
The run explorer reads it; no monitor input holds reasoning.

## The transcript (`loc_arena/logging_/transcript_lanes.py`, `transcript_render.py`)

`transcript.html` and `transcript.txt` are built from the `.eval`: one column per lane (World, then each monitor
with a verdict, then the agents in configured order) and one row per round. Each monitor verdict sits in its
monitor's lane at the row of the event whose `mirror_seq` it targets, else in an `unplaced verdicts` row. The
verdict that decided `caught_by` is marked. The HTML is self-contained, ASCII only, with no JavaScript.

`run_sweep` writes no `.eval` for its episodes.
