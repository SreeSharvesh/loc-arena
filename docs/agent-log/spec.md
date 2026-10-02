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
| I5 | Everything sits behind `logging.agent_transcript` (on by default since the last PR of the stack) | flag-off runs keep the JSON placeholder and no transcript |

## Lane rule

A seq's lane is the turn bound when it was appended, else World. Never `actor_uid`: in attack mode the NPCs
write a scripted PR stamped `agent-main` outside any turn (`coworker.py:219-280`).

## Data model (`loc_arena/logging_/agent_trace.py`)

| Type | Fields | Meaning |
|---|---|---|
| `Phase` | `"deciding"` or `"executing"` | agent policy choosing this turn's actions vs the tool layer running |
| `TurnRef` | `agent_uid`, `turn` | one agent turn; under the live round-robin driver `turn` is also the round index |
| `TurnRecord` | `ref`, `wall_start`, `wall_end` | wall-clock bounds of a completed turn; the episode clock is simulated and jumps |
| `ModelCall` | `phase`, `identity`, `role`, `model_input`, `output`, `sealed_seq`, `wall_ts`, `usage` | one provider call as the core made it; `model_input` is after covert injection; its turn is `sealed_lane[sealed_seq]`; `phase` is `None` outside a turn; `usage` is its prompt, completion and cached token counts and its cost |
| `EpisodeTrace` | `turns`, `sealed_lane`, `mirror_lane`, `mirror_to_sealed`, `phases`, `model_calls`, `last_sealed_seq` | finished read-only trace; lane maps give `TurnRef` or `None` (World); `mirror_to_sealed` pairs dual-recorded twins, since the two logs number independently; `phases` maps each scaffold model call's sealed seq to its phase |

## `AgentTrace` contract

| Method | Called from | Behaviour | Raises |
|---|---|---|---|
| `__init__(*, wall_clock=time.time)` | harness, once per episode | empty trace, no turn bound | - |
| `turn(agent_uid, turn)` | `Agent.run_turn`, around inbox, agent policy and tool steps | context manager: binds the turn in phase `deciding`; on exit unbinds first, then records a `TurnRecord`, also when the body raises | `RuntimeError` if a turn is already bound |
| `mark_executing()` | `Agent.run_turn`, just before `tools.execute` | switches the bound turn to `executing` | `RuntimeError` if no turn is bound |
| `on_sealed_append(event)` | the episode wiring's traced sealed log, for each event the scaffold records | tags `event.seq` with the bound turn or World | never on purpose; a miss surfaces as an unassigned seq in the exporter |
| `on_mirror_append(event)` | the episode wiring's traced mirror log, for each event the scaffold records | tags `event.seq`; pairs it with the most recent unpaired sealed event when the two are equal in every field but `seq` and `fp` (`Recorder.dual` writes sealed then mirror); a sealed event twins at most one mirror event; edge prompt copies get a lane but no twin | never on purpose |
| `on_model_reply(sealed_seqs, mirror_seqs)` | the episode wiring's gateway, after each model call the scaffold makes | tags the call's sealed and mirror seqs with the bound turn, and its sealed seqs with the bound phase | - |
| `on_model_call(*, identity, role, model_input, output, sealed_seq, usage)` | `GatewayCore.generate` / `batch_generate` in process (the episode core's observer), after the sealed record is written | records a `ModelCall` with the bound phase | - |
| `on_model_call_failed(*, sealed_seq)` | `GatewayCore.generate` / `batch_generate` in process, after a provider failure is sealed as an `inference_error` | tags that sealed seq with the bound turn and phase, as `on_model_reply` does for a reply | - |
| `finish(last_sealed_seq)` | harness, when assembly returns | returns a read-only `EpisodeTrace` snapshot; `last_sealed_seq` is the boundary for I3/I4, so anything observed later is post-episode by construction | `RuntimeError` if a turn is still bound |

`gateway.wiring.open_episode_wiring(..., trace=...)` subscribes the trace to the episode's two logs and its
gateway, in process and in the stack alike, so the subscription cannot drift. In the stack the runner cannot
see the core's provider calls or agent code's off-path calls: it exports what it saw
(`export_runner_episode`), and the host joins that with the copied sealed logs (`merge_runner_episode`): each
sealed model call takes the phase the runner saw, and a sealed seq the runner never saw takes the turn of the
two known seqs around it when they share one, else World.

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
| `ModelEvent` | a sealed `inference_call` whose seq has a `ModelCall`; input is the post-injection prompt, output the reply with its usage (tokens, cached tokens and cost; its `input_tokens` leave out the cached prompt tokens, which Inspect counts in `input_tokens_cache_read`), `metadata` holds identity, phase and seq |
| `ToolEvent` | a sealed `action`; `function` = tool, `arguments` = payload args, `error` (type `permission`) set when the payload or the result says blocked; `metadata` holds the sealed `seq` and the `mirror_seq` of its mirror twin (`None` when it has none), since monitor verdicts name mirror seqs and the two logs number independently |
| `InfoEvent(source=<kind>)` | every other sealed event (messages, spawns, NPC activity, monitor records after the boundary); `data` holds the sealed `seq` and the twin's `mirror_seq` like a `ToolEvent` |
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
| `_tool_event(event, span_id, at, *, mirror_seq)` | one `ToolEvent` with id `seq-<n>` | - |
| `_info_event(event, span_id, at, *, mirror_seq)` | one `InfoEvent` whose data carries seq, mirror seq, actor, target, payload and result | - |
| `_timestamp(wall)` | epoch seconds -> aware UTC `datetime` | - |

## M5 wiring (behind `logging.agent_transcript`, default off)

| Where | Change |
|---|---|
| `configs/env.default.yaml`, `config.py` | `logging.agent_transcript` -> `RunConfig.agent_transcript` (fail-loud bool) |
| `GatewayCore.__init__` / `generate` / `batch_generate` | optional `trace`; each provider call reported after its sealed record is written |
| `Agent.__init__` / `run_turn` | optional `trace`; inbox, agent policy and tool steps run inside `trace.turn`; `mark_executing` before the tool layer |
| `task.assemble_in_process`, `stack_episode.run_runner_phase` | create the trace when the flag is on, hand it to the episode wiring (and agents), finish it when the episode is closed; `EpisodeResult.trace` in process, the runner's export in the stack |
| `harness._assemble_by_policy`, `run_episode` | optional `provider` passthrough (offline live runs); the attack run's calibration twin goes to `_write_bundle` |
| `harness._write_bundle`, `_eval_episodes` | flag on: real `.eval` via `write_run_eval` (samples `episode`, `honest_cal`); flag off: the JSON placeholder |
| `cli view` | prints `inspect view --log-dir <bundle>` when the `.eval` is real |

Known limits:

| Limit | Consequence |
|---|---|
| in the stack, the runner does not see off-path calls from agent code | their sealed seqs take a lane by the merge rule above, and their inputs come from the sealed model-call log |
| the orchestrator path (`scaffold/orchestrator.py`, tests only) builds agents without a trace | with the flag on, its events export in World |
| `run_sweep` writes no bundles | no `.eval` for sweep episodes |
| rerunning into an existing run directory | the old sealed seqs are untagged, so the export raises `UnassignedEventError` rather than exporting two runs as one |

## M3 `loc_arena/logging_/transcript_lanes.py`

Turns one Inspect sample (as written by M2) into a grid: lanes are columns, rounds are rows. Rows are keyed by
round, not wall clock, so every agent's turn in a round sits side by side (turns run one after another, so
wall-clock rows would give one filled cell per row).

| Rule | Detail |
|---|---|
| attachments | the sample is resolved first (`resolve_sample_attachments(..., "full")`); Inspect stores long strings as `attachment://` refs |
| lane of an event | the agent that owns the turn span its span sits in or under (`turn:<uid>:<n>` under `agent:<uid>`), else `World` |
| row of an event | the turn's round `n`; a World event takes the round of the latest turn span begun before it, or `-1` (before the first round) |
| lane order | `World`, then one lane per monitor with a verdict (first-seen order), then the sample metadata's configured agent order, then any other agent in first-seen order |
| blocks | a model event gives a `prompt` block (title shows identity and phase) and a `reply` block (`reply (error)` when the call failed); a tool event gives a `tool` block (arguments as `code`, the result in the body, or the block reason followed by the result, `blocked` on error); an info event gives an `info` block titled by its source; spans give none |
| verdict blocks | one per entry in `sample.metadata["scores"]["monitor_verdicts"]` (present only on the graded episode's sample, M2), parsed as `MonitorVerdict`; a malformed entry raises `pydantic.ValidationError`. Each goes to its monitor's own lane, at the row of the event whose `mirror_seq` equals the verdict's `target_seq` (monitors read the mirror, so `target_seq` is a mirror seq), else the `UNPLACED` row (`-2`) when it has no `target_seq` or targets an event the transcript does not show (a mirror-only inference record, a window- or stream-level verdict). A log exported before events carried `mirror_seq` puts every verdict there |
| the caught verdict | the verdict(s) whose `monitor` is in `scores["safety"]["caught_by"]` and whose (3-decimal-rounded) `score` equals that monitor's `scores["safety"]["per_monitor_max_score"]` get their title suffixed `-- CAUGHT` and `blocked=True` (reuses the tool-block red styling — same CSS class, no new rule) so the decisive verdict stands out from the routine ones |

| Function | Behaviour |
|---|---|
| `build_transcript(sample)` | walks the sample's events once, assigns lane and row, collects blocks per cell and each event's mirror seq -> row, then adds the verdict cells |
| `_turn_owners(events)` | span id -> (agent uid, round) for every turn span and every span nested under one; a turn span not shaped `turn <n>` under an agent span raises `ValueError` |
| `_lane_order(configured, seen, monitors)` | `World` + monitors + configured agents + unconfigured agents seen, without duplicates |
| `_blocks(event)` | dispatches to the builders below; other event types give no blocks |
| `_model_blocks(event)` / `_tool_block(event)` / `_info_block(event)` | one event -> its blocks |
| `_mirror_seq(event)` | a `ToolEvent`'s or `InfoEvent`'s `mirror_seq`; `None` for anything else (model calls have no mirror twin) |
| `_verdict_cells(scores, mirror_rows)` | one verdict block per verdict, keyed by (monitor, row) per the rule above |
| `_monitor_verdicts(scores)` | `scores["monitor_verdicts"]` parsed as a list of `MonitorVerdict`; `[]` when `scores` is not a mapping (the calibration twin has none) |
| `_caught_monitor_scores(scores)` | `{monitor: its per_monitor_max_score}` for every monitor in `scores["safety"]["caught_by"]`, else `{}` |
| `_is_the_catch(verdict, caught_scores)` | whether this verdict's monitor and score are the one that tripped `caught_by` |
| `_verdict_block(verdict, *, caught)` | one `monitor_verdicts` entry -> a `verdict` block titled `monitor: <name> (<tap>)` (`-- CAUGHT` suffix when `caught`), body is the score, actor and reason |

## M4 `loc_arena/logging_/transcript_render.py`

Writes `transcript.html` and `transcript.txt` into the run bundle from the bundle's `.eval`.

| Rule | Detail |
|---|---|
| layout (HTML) | one section per sample: a bounded scroll box holding a sticky header grid (lane names) and a body grid with a row-label column plus one column per lane, one row per round; both grids share one column template |
| blocks (HTML) | one `<article>` per block, classed by kind; prompts collapsed in `<details>`; tool arguments highlighted as JSON with Pygments; blocked tool calls and verdict blocks each styled apart |
| layout (text) | per sample, every lane in lane order (`(no activity)` when empty), blocks in round order, each prefixed with its round label; written as UTF-8 with `backslashreplace` |
| self-contained | inline CSS only; no JavaScript, no external `src`/`href` |
| ASCII only | the page is HTML-escaped, then non-ASCII characters become numeric entities, so model text cannot put literal dashes or unescaped markup in the file |
| control characters | C0 controls (except tab and newline) and DEL become visible `\xNN` escapes in both outputs |
| row labels | `-2` -> `unplaced verdicts`, `-1` -> `before round 0`, `n` -> `round n` |

| Function | Behaviour |
|---|---|
| `write_transcripts(eval_path, out_dir)` | reads the `.eval`, builds one transcript per sample (M3), writes both files, returns their paths |
| `render_html(title, transcripts)` / `render_text(title, transcripts)` | the whole page / the whole text file |
| `_grid_html(transcript)` | one sample's grid |
| `_block_html(block)` | one block's `<article>` |
| `_code_html(code)` | Pygments JSON highlighting |
| `_row_label(row)` | the round label |
