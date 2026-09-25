# Per-agent Inspect logging: spec

Status of each function is tracked by `grep -rn NotImplementedError loc_arena/logging_/`.

## Goal

Every run writes a real Inspect `.eval` log with one span per agent, and renders it as a transcript with one
lane per agent. Each lane shows what the agent saw, what it said, what it did, and every sealed event its
turns caused. Today the `.eval` is a JSON stub (`harness.py:421-432`).

## Why attribution works

An episode runs one agent turn at a time on one thread (`live.py:271-279`, `orchestrator.py:113-129`). Every
event appended while a turn is bound was caused by that turn. Every event appended with no turn bound belongs
to the World lane (NPCs, ticks, orchestrator closes).

## Invariants

| # | Invariant | Reason |
|---|---|---|
| I1 | Sealed and mirror log bytes never change | the verifier, the monitors and the byte-identical repro test (`test_end_to_end.py:72-76`) read them |
| I2 | The trace is held in memory and written only in `_write_bundle`, after both episodes | agent-run `run_tests` can read the run directory while an episode is live |
| I3 | Every sealed seq up to the episode boundary maps to exactly one lane; the exporter raises otherwise | `AppendOnlyLog.on_append` swallows subscriber exceptions (`events.py:212-216`) |
| I4 | Sealed events after the boundary are post-episode and go to World | monitor calls are appended later by a second writer with no subscriber (`harness.py:667`) |
| I5 | Everything sits behind `logging.agent_transcript` (off) until the last PR | branches in the middle of the stack stay green |

## Lane rule

A seq's lane is the turn bound when it was appended, else World. Never `actor_uid`: in attack mode the NPCs
write a scripted PR stamped `agent-main` outside any turn (`coworker.py:219-280`).

## Data model (`loc_arena/logging_/agent_trace.py`)

| Type | Fields | Meaning |
|---|---|---|
| `Phase` | `"deciding"` or `"executing"` | brain choosing an action (including parse retries) vs the tool layer running |
| `TurnRef` | `agent_uid`, `turn` | one agent turn; under the live round-robin driver `turn` is also the round index |
| `TurnRecord` | `ref`, `wall_start`, `wall_end` | wall-clock bounds of a completed turn; the episode clock is simulated and jumps |
| `ModelCall` | `ref`, `phase`, `identity`, `role`, `model_input`, `output`, `sealed_seq`, `wall_ts` | one provider call as the core made it; `model_input` is after covert injection; `ref` and `phase` are `None` outside a turn |
| `EpisodeTrace` | `turns`, `sealed_lane`, `mirror_lane`, `mirror_to_sealed`, `model_calls`, `last_sealed_seq` | finished read-only trace; lane maps give `TurnRef` or `None` (World); `mirror_to_sealed` pairs dual-recorded twins, since the two logs number independently |

## `AgentTrace` contract

| Method | Called from | Behaviour | Raises |
|---|---|---|---|
| `__init__(*, wall_clock=time.time)` | harness, once per episode | empty trace, no turn bound | - |
| `turn(agent_uid, turn)` | `Agent.run_turn`, around inbox, brain and tool steps | context manager: binds the turn in phase `deciding`; on exit records a `TurnRecord` and unbinds, also when the body raises | `RuntimeError` if a turn is already bound |
| `mark_executing()` | `Agent.run_turn`, just before `tools.execute` | switches the bound turn to `executing` | `RuntimeError` if no turn is bound |
| `on_sealed_append(event)` | sealed `AppendOnlyLog(on_append=...)` | tags `event.seq` with the bound turn or World | never on purpose; a miss surfaces as an unassigned seq in the exporter |
| `on_mirror_append(event)` | mirror `AppendOnlyLog(on_append=...)` | tags `event.seq`; pairs it with the most recent sealed event when kind, actor, turn, tool, timestamp and payload match (`Recorder.dual` writes sealed then mirror); edge prompt copies get a lane but no twin | never on purpose |
| `on_model_call(*, identity, role, model_input, output, sealed_seq)` | `GatewayCore.generate` / `batch_generate`, episode instance only | records a `ModelCall` with the bound turn and phase | - |
| `finish(last_sealed_seq)` | harness, when assembly returns | closes the trace and returns `EpisodeTrace`; `last_sealed_seq` is the boundary for I3/I4 | `RuntimeError` if a turn is still bound |

## Verified assumptions

| Assumption | Evidence |
|---|---|
| one sealed and one mirror writer per episode | `live.py:153-154`, `task.py:268-269`; the only other writer is post-episode (`harness.py:667`) |
| turns never nest | the orchestrator drains children after the root turn returns (`orchestrator.py:131-136`) |
| inspect-ai 0.3.268 has agent spans, `ToolEvent.agent_span_id`, and log round-trip | probe on the locked version |

## Later modules (specified when reached)

| Module | Responsibility |
|---|---|
| M2 `inspect_export.py` | `EpisodeTrace` + sealed log + scores -> Inspect `EvalLog`: a sample per episode, a `type="agent"` span per agent |
| M3 `transcript_lanes.py` | Inspect sample -> ordered lanes of blocks |
| M4 `transcript_render.py` | lanes -> `transcript.html` + `transcript.txt` |
| M5 wiring | hooks above, bundle write, offline provider injection, `inspect view --log-dir` hint |
