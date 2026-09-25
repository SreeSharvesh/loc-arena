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
| `ModelCall` | `phase`, `identity`, `role`, `model_input`, `output`, `sealed_seq`, `wall_ts` | one provider call as the core made it; `model_input` is after covert injection; its turn is `sealed_lane[sealed_seq]`; `phase` is `None` outside a turn |
| `EpisodeTrace` | `turns`, `sealed_lane`, `mirror_lane`, `mirror_to_sealed`, `model_calls`, `last_sealed_seq` | finished read-only trace; lane maps give `TurnRef` or `None` (World); `mirror_to_sealed` pairs dual-recorded twins, since the two logs number independently |

## `AgentTrace` contract

| Method | Called from | Behaviour | Raises |
|---|---|---|---|
| `__init__(*, wall_clock=time.time)` | harness, once per episode | empty trace, no turn bound | - |
| `turn(agent_uid, turn)` | `Agent.run_turn`, around inbox, brain and tool steps | context manager: binds the turn in phase `deciding`; on exit unbinds first, then records a `TurnRecord`, also when the body raises | `RuntimeError` if a turn is already bound |
| `mark_executing()` | `Agent.run_turn`, just before `tools.execute` | switches the bound turn to `executing` | `RuntimeError` if no turn is bound |
| `on_sealed_append(event)` | sealed `AppendOnlyLog(on_append=...)` | tags `event.seq` with the bound turn or World | never on purpose; a miss surfaces as an unassigned seq in the exporter |
| `on_mirror_append(event)` | mirror `AppendOnlyLog(on_append=...)` | tags `event.seq`; pairs it with the most recent unpaired sealed event when the two are equal in every field but `seq` and `fp` (`Recorder.dual` writes sealed then mirror); a sealed event twins at most one mirror event; edge prompt copies get a lane but no twin | never on purpose |
| `on_model_call(*, identity, role, model_input, output, sealed_seq)` | `GatewayCore.generate` / `batch_generate`, episode instance only, after the sealed record is written | records a `ModelCall` with the bound phase | - |
| `finish(last_sealed_seq)` | harness, when assembly returns | returns a read-only `EpisodeTrace` snapshot; `last_sealed_seq` is the boundary for I3/I4, so anything observed later is post-episode by construction | `RuntimeError` if a turn is still bound |

## Verified assumptions

| Assumption | Evidence |
|---|---|
| one sealed and one mirror writer per episode | `live.py:153-154`, `task.py:268-269`; the only other writer is post-episode (`harness.py:667`) |
| turns never nest | the orchestrator drains children after the root turn returns (`orchestrator.py:131-136`) |
| inspect-ai 0.3.268 has agent spans, `ToolEvent.agent_span_id`, and log round-trip | probe on the locked version |

## M2 `loc_arena/logging_/inspect_export.py`

One `.eval` per run. Each episode becomes one sample: `episode` (the graded run) and, in attack mode,
`honest_cal` (the calibration twin). `episodes[0]` is the graded episode and is the only one given `scores`.

| Inspect element | Built from |
|---|---|
| `SpanBeginEvent(type="episode", name=<sample id>, id="episode:<sample id>")` | the sample's root span: first and last event; parent of every agent span and span of every World event, because Inspect's viewer attaches parentless spans and span-less events to whatever span is open |
| `SpanBeginEvent(type="agent", name=<uid>, id="agent:<uid>", parent_id=<root>)` | opened at the agent's first turn; all agent spans close at the end of the sample |
| `SpanBeginEvent(type="turn", name="turn <n>", id="turn:<uid>:<n>", parent_id="agent:<uid>")` | one per turn that wrote at least one sealed event; opened and closed around that turn's contiguous run of sealed seqs; a turn appearing in two runs raises `ValueError` |
| `ModelEvent` | a sealed `inference_call` whose seq has a `ModelCall`; input is the post-injection prompt, output the reply, `metadata` holds identity, phase and seq |
| `ToolEvent` | a sealed `action`; `function` = tool, `arguments` = payload args, `error` (type `permission`) set when the payload or the result says blocked |
| `InfoEvent(source=<kind>)` | every other sealed event (messages, spawns, NPC activity, monitor records after the boundary) |
| event `span_id` | the turn span of the event's lane, or the root span for World |
| event `timestamp` | a running wall clock: turn start, then each model call's `wall_ts`, then turn end; World events reuse the last known time |
| event `working_start` | seconds since the root span's timestamp (Inspect would otherwise use machine uptime) |

| Function | Behaviour | Raises |
|---|---|---|
| `write_run_eval(path, *, run_name, config, mode, seed, scores, episodes)` | builds the `EvalLog` (spec + one sample per episode, status `success`) and writes it with `inspect_ai.log.write_eval_log`; returns `path` | - |
| `_eval_spec(run_name, config, mode, seed, sample_count)` | task `loc_arena/<run_slug>`, `run_id` = run name, model = the `untrusted_agent` model id, metadata mode/seed/policy | `KeyError` if the config has no `untrusted_agent` model |
| `_sample(episode, scores)` | reads the sealed log, assigns lanes, builds the events; metadata holds the agent order and the scores | `UnassignedEventError` via `_lanes_for` |
| `_lanes_for(trace, sealed_events)` | seq <= `last_sealed_seq`: the trace's lane; later seqs: World | `UnassignedEventError` naming the untagged seqs inside the boundary |
| `_sample_events(episode, sealed_events, lanes)` | walks sealed events in seq order as runs of one lane and emits spans and events per the table above; agent spans open in first-turn order (the configured order lives in sample metadata for the lane view) | `ValueError` on an interleaved turn; `KeyError` on a lane with no `TurnRecord` |
| `_episode_span_id(sample_id)` / `_agent_span_id(uid)` / `_turn_span_id(ref)` | `episode:<id>` / `agent:<uid>` / `turn:<uid>:<n>` | - |
| `_model_event(call, span_id)` | one `ModelEvent`, model and role = the call's model role | - |
| `_tool_event(event, span_id, at)` | one `ToolEvent` with id `seq-<n>` | - |
| `_info_event(event, span_id, at)` | one `InfoEvent` whose data carries seq, actor, target, payload and result | - |
| `_timestamp(wall)` | epoch seconds -> aware UTC `datetime` | - |

## Later modules (specified when reached)

| Module | Responsibility |
|---|---|
| M3 `transcript_lanes.py` | Inspect sample -> ordered lanes of blocks |
| M4 `transcript_render.py` | lanes -> `transcript.html` + `transcript.txt` |
| M5 wiring | hooks above, bundle write, offline provider injection, `inspect view --log-dir` hint |
