# pi as the agents' harness

`harness: pi` in a run config runs each agent's whole session in [pi](https://github.com/earendil-works/pi), the
coding agent the company's own agents run in on the runners, in place of the arena's own loop
(`harness: native`, the default). The episode starts every agent's loop at once and plays no turn. It runs in a
stack run only.

`loc_arena/own_sessions.py` · `loc_arena/workspace_service.py` · `loc_arena/session_events.py` · `loc_arena/gateway/loop_routes.py`

## What a loop is

Each agent's loop is one pi process in the episode's container, with its own home: its brief as pi's system
prompt, pi's settings and its session. A loop can do two things, and each passes a gateway that records it.

| A loop | Where | As | Recorded by |
| --- | --- | --- | --- |
| asks its model | the model gateway, on its agent's own route (`/loop/<agent id>/api/v1`) | the episode's caller | the gateway's call record (`gateway.calls.jsonl`) |
| calls a tool | the tools gateway (`/mcp`) | its agent, with the agent's sandbox token | the service that takes the call |

The gateway sends each model call upstream with the request fields the run config sets for the agents' role:
its temperature, its `max_tokens` (as `max_completion_tokens`, the field pi sends), its model's provider routing
and its reasoning, whatever pi sent for them.

A loop runs nothing itself: pi's own tools and its codemode are off. So nothing a model writes runs beside the
other agents' loops, briefs and keys.

## The agent's tools

The agent is offered its `sandbox: tools:` on the company's services, and the workspace service's tools: `bash`,
`read_file`, `write_file`, `edit_file`, `list_dir` and `search_code`. pi names each `mcp__meridian__<tool>`.

The workspace service runs in the episode's container, on the code tools the native loop calls: a file tool
reads and writes the agent's checkout, and a command runs in the agent's sandbox. The tools gateway names the
agent to it with that agent's workspace key, derived from its sandbox token. It runs one call of an agent at a
time, and records every call in `records/workspace.jsonl` of the play's directory. At the end of play it refuses
new calls and waits for a command still running, so nothing acts after the checkouts are captured.

## How a session ends

pi's run ends when its model answers without a tool call. The session then goes on, on a nudge, `pi.resumes`
times. `session_ends.json` in the play's directory holds each agent's end, with pi's exit code and the end of
its error output. Each loop's home is kept in `loops/<agent id>`.

| End | When |
| --- | --- |
| `finished` | the model answered without a tool call once more than `pi.resumes` |
| `budget` | the gateway refused a model call past the agent's turn budget (`episode.orchestrator_turn_budget` for the lead, `episode.subagent_turn_budget` for the others): its route admits that many requests in a play. A request the provider failed, pi's retry of it and a call pi makes to summarize a long session each count as one |
| `ceiling` | `episode.wall_clock_seconds` passed |
| `failed` | pi failed otherwise, past its own retries. It counts in the run's `failed_model_calls` |

## Events

The post-play builder (`loc_arena.recorded_events`) makes a session's events from the two records.

| Event | From |
| --- | --- |
| `inference_call` | each model call of the loop the gateway answered, a call that summarizes a long session among them: the agent's turn of that number, from 1. Fingerprints alone; the prompt's is over the messages after the system prompt, which holds the brief. Its `credential_kind` is `turn_token`, the tag of a sanctioned turn: the agent's own route admitted the call |
| `action` of a workspace tool | the workspace service's record, with the call's arguments and result |
| `action`, `message`, `pr`, `job` of a company tool | that service's record, as under the native loop |
| `checkout` (sealed log only) | the episode, as cloned and at the end of play |

A tool call is placed in the turn whose span holds its time: from the agent's model call to its next one. A
model request that offered a tool the loop would run itself is reported in `unattributed_records.jsonl`.

## Differences from the native loop

| | `harness: native` | `harness: pi` |
| --- | --- | --- |
| Scheduling | the episode plays the agents' turns in rounds | every loop runs at its own pace, at once |
| Bounds | a turn budget per agent, the wall clock | the same turn budget, as model calls at the gateway; `pi.resumes`; the wall clock |
| New chat and tickets | handed to the agent at its turn's start (`chat.deliver_unread`, `tickets.deliver_assigned`) | read by the agent with its tools; those two settings do nothing |
| Checkout captures | after each native call | as cloned and at the end of play |
| The covert objective (`covert.objective_prompt`) | the gateway core puts it before the target agent's first message, on each call | the episode puts it before the target agent's brief, in pi's system prompt |
| A call from an agent's sandbox to the model gateway | off-path | off-path |
