# pi as the agents' harness

`harness: pi` in a run config runs each agent's whole session in [pi](https://github.com/earendil-works/pi), the
coding agent the company's own agents run in on the runners, in place of the arena's own loop
(`harness: native`, the default). The episode starts every agent's loop at once and plays no turn. It runs in a
stack run only.

`loc_arena/own_sessions.py` · `loc_arena/notices.py` · `loc_arena/workspace_service.py` · `loc_arena/session_events.py` · `loc_arena/gateway/loop_routes.py`

## What a loop is

Each agent's loop is one pi process in the episode's container for the whole play, in pi's RPC mode, with its
own home: its brief as pi's system prompt, pi's settings and its session. A loop can do two things, and each passes a gateway that records it.

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
`read_file`, `write_file`, `edit_file`, `list_dir` and `search_code`, and its subscription tools (below). pi
names each `mcp__meridian__<tool>`.

The workspace service runs in the episode's container, on the code tools the native loop calls: a file tool
reads and writes the agent's checkout, and a command runs in the agent's sandbox. The tools gateway names the
agent to it with that agent's workspace key, derived from its sandbox token. It runs one call of an agent at a
time, and records every call in `records/workspace.jsonl` of the play's directory. At the end of play it refuses
new calls and waits for a command still running, so nothing acts after the checkouts are captured.

## Notices

A loop starts on its task. When its model answers without a tool call the loop is idle. Only a notice starts it
again: one line that says something happened in a topic the agent subscribed to. What happened is for the agent
to read with its tools. Nothing else prompts a loop, and an agent gets no notice of what it did itself.

| Topic | A notice when |
| --- | --- |
| `dm` | another member sent the agent a direct message |
| `job` | a job of the agent changed state |
| `ticket` | a ticket was assigned to the agent |
| `channel:<name>` | another member wrote in that channel |
| `repo:<name>` | another member opened or merged a pull request in that repo |

Each agent starts with the topics of `pi.subscriptions` (`dm`, `job`, `ticket`) and changes its own with
`subscribe`, `unsubscribe` and `list_subscriptions`, served with the workspace tools and recorded like them.

| Step | Where |
| --- | --- |
| 1. A service records a call, a request or a job's change of state | its log, and its file on the records volume (`stack.records_directory`), which only the services and the episode mount |
| 2. The episode reads the new lines every `pi.notice_poll_seconds` and builds their events | the mapping the post-play builder uses (`loc_arena.recorded_events`) |
| 3. Each event becomes a notice for each agent subscribed to its topic | `notices.jsonl` in the play's directory: the agent, the topic, the text, and `sent`, false when the agent's session had ended |
| 4. The episode gives the notice to the agent's pi as a prompt | an idle loop starts a run on it; a working loop gets it before its next model call |

## How play and a session end

Play ends when every loop is idle and the services' records hold nothing new, or at the wall-clock ceiling. A
job still running then ends with the run: an agent that needs its result waits for it. `session_ends.json` in
the play's directory holds each agent's end, with pi's exit code and its last error. Each loop's home is kept in
`loops/<agent id>`.

| End | When |
| --- | --- |
| `finished` | the loop was idle when play ended. An answer cut at the model's output cap leaves a loop idle like any other |
| `budget` | the gateway refused a model call past the agent's turn budget (`episode.orchestrator_turn_budget` for the lead, `episode.subagent_turn_budget` for the others): its route admits that many requests in a play. A request the provider failed, pi's retry of it and a call pi makes to summarize a long session each count as one. The session takes no later notice |
| `ceiling` | the loop was still working when `episode.wall_clock_seconds` passed |
| `failed` | pi failed otherwise, past its own retries, or exited. It counts in the run's `failed_model_calls` |

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
| Bounds | a turn budget per agent, the wall clock | the same turn budget, as model calls at the gateway; the wall clock |
| New chat and tickets | handed to the agent at its turn's start (`chat.deliver_unread`, `tickets.deliver_assigned`) | read by the agent with its tools, on a notice or when it chooses; those two settings do nothing |
| Checkout captures | after each native call | as cloned and at the end of play |
| The covert objective (`covert.objective_prompt`) | the gateway core puts it before the target agent's first message, on each call | the episode puts it before the target agent's brief, in pi's system prompt |
| A call from an agent's sandbox to the model gateway | off-path | off-path |
