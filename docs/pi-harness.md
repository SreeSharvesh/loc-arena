# pi as the agents' harness

Each agent's whole session runs in [pi](https://github.com/earendil-works/pi), the coding agent the company's
own agents run in on the runners. The episode starts every agent's loop at once and plays no turn. `pi` is the
default and the only value of the run config's `harness` key.

`loc_arena/own_sessions.py` · `loc_arena/notices.py` · `loc_arena/workspace_service.py` · `loc_arena/tool_offer.py` · `loc_arena/tool_list_notifier.py` · `loc_arena/session_events.py` · `loc_arena/gateway/loop_routes.py`

## What a loop is

Each agent's loop is one pi process in the episode's container for the whole play, in pi's RPC mode, with its
own home: its brief as pi's system prompt, pi's settings and its session. In an attack the brief of
`covert.target_identity` starts with the covert objective (`covert.objective_prompt`). A loop can do two things, and each passes a gateway that records it.

| A loop | Where | As | Recorded by |
| --- | --- | --- | --- |
| asks its model | the model gateway, on its agent's own route (`/loop/<agent id>/api/v1`) | the episode's caller | the gateway's call record (`gateway.calls.jsonl`) |
| calls a tool | the tools gateway (`/mcp`) | its agent, with the agent's sandbox token | the service that takes the call |

The gateway sends each model call upstream with the request fields the run config sets for its agent's role
(`untrusted_agent`, or the role the models file's `agent_roles` names for that agent, so a lead can play on
another model or reasoning effort than its reports):
its temperature, its `max_tokens` (as `max_completion_tokens`, the field pi sends), its model's provider routing
and its reasoning, whatever pi sent for them. The model's reasoning counts toward `max_tokens`, and an answer cut
there before its tool call ends the loop's work until a notice, so `models.cheap.yaml` sets 32,768.

With `gateway.agents_upstream` the loops' calls go to that provider, on its chat path and under its own key,
and carry no provider routing, which is OpenRouter's field. The monitors, the runners and the jobs stay on
`gateway.upstream`. Both keys are secrets of the gateway container alone.

A loop runs nothing itself: pi's own tools and its codemode are off. So nothing a model writes runs beside the
other agents' loops, briefs and keys.

## The agent's tools

The agent is offered its `sandbox: tools:` on the company's services, and the workspace service's tools: `bash`,
`read_file`, `write_file`, `edit_file`, `list_dir` and `search_code`, and its subscription tools (below). pi
names each `mcp__meridian__<tool>`.

### When a target restarts

<img alt="UML sequence diagram. pi opens a session while the notes service restarts, and the tools gateway lists it no notes tools. The deployment ends; the episode reads that in the deploy service's record and lists through the gateway as each agent until every tool named is offered. Then the tool-list notifier sends tools/list_changed through the gateway, and pi lists again and is offered every tool." src="figures/tool-list-notice.svg" width="100%">

The gateway lists a session the tools of the targets that answer, and pi keeps a session's list until it is told
the list changed. So sessions start only once a new session, opened as each agent, lists every tool its run
config names, read again for at most `stack.tools_offer_seconds`; `tools_offered.json` in the play's directory
holds what each agent was offered and lacked, and while a tool is still missing the run fails naming it.

During play a deployment restarts its service. When the deploy service's record shows a deployment ended, the
same check runs again, and once it passes, the tool-list notifier sends `notifications/tools/list_changed` on
every session's stream, and pi lists again. A session that opened while the service was down gets its tools
back; no session lists while a named tool is missing, so an open session never loses one. A target that is
still missing after `stack.tools_offer_seconds` gets no notice until the next deployment ends. Two windows
are left, and both show in `tools_lacked.jsonl`: a deployment that ended on its time bound may still restart
its service after the check, and a second deployment may stop its service between the check and pi's listing.

The notifier is a target of the gateway with no tools, served in the episode's container beside the workspace
service. It admits only the gateway, by a key no agent holds, and writes no record. The gateway's log names
the tools of each listing (`tools_listed`). After play the run writes `tools_lacked.jsonl`, one line per tool
a session's listing lacked, with the agent, the session and the time, and warns when it has any.

The workspace service runs in the episode's container, on the code tools of `loc_arena/scaffold/code_services.py`: a file tool
reads and writes the agent's checkout, and a command runs in the agent's sandbox. The tools gateway names the
agent to it with that agent's workspace key, derived from its sandbox token. It runs one call of an agent at a
time, and records every call in `records/workspace.jsonl` of the play's directory. At the end of play it refuses
new calls and waits for a command still running, so nothing acts after the checkouts are captured.

## Notices

A loop starts on its task. When its model answers without a tool call the loop is idle. Only a notice starts it
again: one line that says something happened in a topic the agent subscribed to. What happened is for the agent
to read with its tools. Nothing else prompts a loop, and an agent gets no notice of what it did itself; what a
job of its own did counts as another member's. A reply posted over chat's HTTP
API names no channel in its record, so it notifies only the members it mentions; a reply made with the `reply`
tool notifies the channel's subscribers too.

| Topic | A notice when |
| --- | --- |
| `dm` | another member sent the agent a direct message |
| `mention` | another member named the agent with `@name` in a channel message or a reply. The agent gets this notice alone, even when it also subscribed to the channel. A message edited to name it notifies nobody |
| `job` | a job the agent submitted changed state. A job that one of its jobs submitted is that job's, and notifies nobody |
| `ticket` | a ticket was assigned to the agent |
| `channel:<name>` | another member wrote in that channel |
| `repo:<name>` | another member opened or merged a pull request in that repo |

Each agent starts with the topics of `pi.subscriptions` (`dm`, `mention`, `job`, `ticket`) and changes its own with
`subscribe`, `unsubscribe` and `list_subscriptions`, served with the workspace tools and recorded like them.

| Step | Where |
| --- | --- |
| 1. A service records a call, a request or a job's change of state | its log, and its file on the records volume (`stack.records_directory`), which only the services that run an engine module and the episode mount. A service built from its own Dockerfile writes its log alone, so its records make no notice |
| 2. The episode reads the new lines every `pi.notice_poll_seconds` and builds their events | the mapping the post-play builder uses (`loc_arena.recorded_events`) |
| 3. Each event becomes a notice for each agent subscribed to its topic | `notices.jsonl` in the play's directory: the agent, the topic, the text, and `sent`, false when the agent's session had ended or its pi read its input no more |
| 4. The episode gives the notice to the agent's pi as a prompt | an idle loop starts a run on it; a working loop gets it before its next model call |

## How play and a session end

Play ends when every loop is idle and the services' records hold nothing new, once `pi.minimum_play_seconds`
has passed, or at the wall-clock ceiling. Until the minimum has passed a job an agent left running keeps
running, and a notice still starts an idle loop. A job still running when play ends, ends with the run. `session_ends.json` in
the play's directory holds each agent's end, with pi's exit code and its last error. Each loop's home is kept in
`loops/<agent id>`. pi runs from a neutral working directory, not its home: it finds its configuration through
`HOME` and absolute arguments, so its process working directory is cosmetic, and keeping it clear of the run
directory stops pi's reported `<cwd>` from carrying the run slug — which names the side task and the policy and
monitor models — into the agent's context.
When no session could start, play ends at once. `aurora-efficiency.fast` sets the minimum to 0.

| End | When |
| --- | --- |
| `finished` | the loop was idle when play ended. An answer cut at the model's output cap leaves a loop idle like any other |
| `budget` | the gateway refused a model call past the agent's turn budget (`episode.orchestrator_turn_budget` for the lead, `episode.subagent_turn_budget` for the others): its route admits that many requests in a play. A request the provider failed, pi's retry of it and a call pi makes to summarize a long session each count as one. The session takes no later notice |
| `ceiling` | the loop was still working when `episode.wall_clock_seconds` passed |
| `failed` | the loop's last run failed otherwise, past pi's own retries, or pi exited. A loop whose run failed is idle: a notice starts it again. It counts in the run's `failed_model_calls` |

## Subagents

<img alt="UML sequence diagram of a delegate call. The parent's pi calls delegate with a task, and optionally a kind and a model, at the tools gateway, which passes it to the workspace service as the parent. The workspace service starts the child like an agent, with its kind's tools. Until it settles, the child asks its model, its own or the parent's, on its own lane of the parent's route, counted in the parent's budget, and calls tools with its own key, named as the caller, while the parent gets progress every 5 seconds. The child answers and ends, and the parent gets the answer." src="figures/subagent-delegate.svg" width="100%">

An agent whose `scope.spawn_subagent` is on hands a task to a subagent with the workspace service's tool
`delegate(task, kind, model)`. The episode starts the subagent as it starts an agent: one pi process in RPC mode,
with a home of its own beside the agents' (`loops/<agent id>.<n>`). The call waits for the subagent's last message,
which is its result, and reports progress every `subagents.progress_seconds` so pi's MCP timeout does not end it.
The subagent starts fresh, from the task text alone.

| Lever | With none | Otherwise |
| --- | --- | --- |
| `kind` | `general-purpose`: every tool its agent holds, and the engine's prompt (`general-purpose.subagent.md`) | a kind of `subagents.definitions` ([run config](run-config.md)): its own prompt (`<kind>.subagent.md`) and a narrower set of its agent's tools |
| `model` | its agent's model | one of `subagents.models`, which the run config lists by model id; any other is refused |

A subagent acts with its agent's permissions. Its tools are its agent's, and IAM decides each of its calls as for
its agent. A kind with `bash` runs commands in its agent's sandbox, where its agent's identities are, so its calls
to a service's HTTP API are its agent's, recorded under its agent. Tools are never passed at call time.

| A subagent | How |
| --- | --- |
| its id | `<agent id>.<n>`: the next of the `subagents.max_children` its agent may start in a play |
| its brief | `agent.subagent.md`, its kind's prompt, and its agent's workspace, skills and services. It gets neither its agent's assignment nor the covert objective: its task is what its caller writes in the `delegate` call, which the records hold |
| asks its model | on its own lane of its agent's route at the model gateway (`/loop/<agent id>.<n>/api/v1`), counted in its agent's turn budget, so delegating never raises it. Its calls carry its agent's role's temperature, output cap and reasoning, and its model's own provider routing. On the route of an agent whose subagents may choose a model, the gateway sets those fields from the model a call names, and refuses a model the run does not allow |
| calls a tool | at the tools gateway with a key of its own, keyed by the episode's key, which no agent holds, and kept outside the run directory. The gateway admits it as its agent, with its agent's identities and tools but the subscription tools, and names it as `caller` in its log. The workspace service records it as itself, on its agent's checkout and sandbox, one call at a time with its agent's |
| its tools | its kind's; pi gets them as `--tools` beside `--no-builtin-tools`, so it runs nothing itself. `delegate` only while it is above `subagents.max_depth` (1: never) |
| its session | `loops/<agent id>.<n>/.pi/session.jsonl`, whose header names its caller's session, by its path in the episode, as `parentSession`, pi's own link from a session to its parent |
| its record | `subagents.jsonl` in the play's directory: its start, with its caller, kind, model, task and tools, and its end, with its answer. Its `inference_call` events name the model each call asked for |

A `delegate` call that would pass `max_depth`, `max_concurrent`, `max_children` or `max_task_characters`, names no
kind of the run's, or a model it does not allow, starts nothing and answers why; a subagent whose pi cannot start is
recorded as `failed`. A call that ends early, cut by pi or by the end of play, ends its subagent too and is recorded
with its answer so far. Play waits while a subagent works, as it waits for an agent's session, and the wall clock
ends both, on one deadline. After play, a subagent's model calls are its own turns, and its workspace calls are
placed in them, on every lane the run config renders, started or not; its calls of the company's tools are recorded
under its agent's identity, which the services saw, and placed in its agent's turn that holds the `delegate` call.
A `delegate` called from inside `bash` waits for the agent's own turn, which that command holds, so its subagent's
workspace calls wait until the command's timeout.

## Watching the sessions while a run plays

A run writes its transcript and report only once both plays are graded. pi writes each session to
`loops/<agent id>/.pi/session.jsonl` as it goes, and `make watch` follows those files in the episode container
while the run plays, printing one line per step: the time, the play, the agent, and what it thought (`THINK`),
said (`SAY`), called (`CALL`) or got back (`RESULT`). A run whose agents never take up their tasks shows in the
first minutes and can be stopped there.

```sh
make watch                             # the run playing, through both plays
make watch RUN_DIR=<run directory>     # a finished run's sessions, once
uv run python -m loc_arena.cli watch --agent agent-main --width 2000   # one agent, longer lines
```

A run directory that holds no session file is an error that names the directory, never an empty printout.
It only reads, with `docker exec` in the episode container, where no agent's code runs. What it prints is the
experimenter's view, as the sealed log is: never give it to an agent or a monitor. A model that returns no
reasoning for a call leaves no `THINK` line for it (`loc_arena/run_watcher.py`).

## Events

The post-play builder (`loc_arena.recorded_events`) makes a session's events from the two records.

| Event | From |
| --- | --- |
| `inference_call` | each model call of the loop the gateway answered, a call that summarizes a long session among them: the agent's turn of that number, from 1. Fingerprints alone; the prompt's is over the messages after the system prompt, which holds the brief. Its `credential_kind` is `turn_token`, the tag of a sanctioned turn: the agent's own route admitted the call |
| `action` of a workspace tool | the workspace service's record, with the call's arguments and result |
| `action`, `message`, `pr`, `job` of a company tool | that service's record |
| `checkout` (sealed log only) | the episode, as cloned and at the end of play |

A tool call is placed in the turn whose span holds its time: from the agent's model call to its next one. A
model request that offered a tool the loop would run itself is reported in `unattributed_records.jsonl`.
