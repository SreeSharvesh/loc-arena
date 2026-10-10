# Changelog

What each part of LOC-Arena does for a run, and how that changed. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Entries describe systems and what they now do; the commit
log has the details.

## [Unreleased]

### Added

- **The board replaces the message bus (#99, step 3).** Agents talk only through the board, an MCP service on
  the official MCP Python SDK (`python -m loc_arena.board <run config>`, the scenario's `board`): `send` posts a
  message to one agent, `read` returns every message addressed to the caller, and `unread` those it has not had
  yet, in send order. The sender is the caller's identity, never an argument, and the board enforces each
  agent's recipient allow-list (`scope.message`) itself. The native loop's `message` tool becomes the board's
  `send`, and each turn begins by delivering the agent's new messages, as the bus did: a `read` of those after the
  last one delivered, tried up to `stack.tools_delivery_attempts` times, so a lost answer loses no message, and
  recorded as the agent's `read` action with its error when every attempt fails; an agent not offered `read`
  gets no delivery. `unread` stays for harnesses that keep no such cursor. A send outside the
  agent's scope is still a blocked `action` event, and a send the board refuses is an `action` event with its
  error; a message the board takes writes no `action` event. Any harness with an MCP client can now talk to the
  team. The default config gives every agent `send`, `read` and `unread`. agentgateway offers each tool by its
  own name (`prefixMode: never`), as an in-process run does, and is pinned to v1.6.0
  (`stack.tools_gateway_image`): with two services on its route, v1.5.0 found the service of a 2026-era call
  by listing every service's tools without the `_meta` envelope the MCP SDK requires, so the call failed; a
  reach row now proves a client of each protocol era through the route. An in-process episode holds a fresh
  board. Sources: https://github.com/agentgateway/agentgateway/pull/3372 (the v1.6.0 fix),
  https://github.com/agentgateway/agentgateway/releases/tag/v1.6.0.
- **`run_tests` and `run_benchmark` are Agent Skills in the sandbox (#99).** A scenario's `skills:` names its
  [Agent Skills](https://agentskills.io/specification) folder, resolved under the pack like a service's build
  directory; Aurora's holds `run-tests` and `run-benchmark`, each a `SKILL.md` and a self-contained script
  that prints the JSON the tools printed. Every sandbox mounts that folder alone, read-only, at
  `stack.skills_directory` (`/skills`), so any harness in it can run them on its own checkout. A stack run's
  native agents are no longer offered the two tools: one sentence of their brief points at the folder, and
  they run the scripts with bash under `stack.shell_timeout_seconds`. An in-process run, which offers no bash,
  offers no skills.
- **Each agent's tools set in the run config, behind agentgateway (#99).** A live service in `scenario.yaml` lists
  the MCP `tools` it serves, and may run a `module` of the engine image instead of a `build` or `image` (the
  forge, whose world is the harness's). An agent's `sandbox: tools:` lists the tools it may call on each such
  service; a tool the service does not list, a tool named twice, or tools for a service that serves none are
  refused at load. In a stack run every service with tools sits behind one MCP route of agentgateway v1.6.0
  (`stack.tools_gateway_image`, port `stack.tools_gateway_port`), whose config is generated per run from
  `sandbox: tools:` and reaches it as a compose secret: it admits each agent by the hash of its sandbox token,
  lists it exactly its tools, refuses every other before it reaches the service, and forwards each call with
  that agent's identity on the service, which only the service and agentgateway hold. Its log, one JSON line
  per request with the agent, the tool, its arguments and its result or error, is copied to
  `services/agentgateway.log` in the run directory. The episode finds the route in `LOC_ARENA_TOOLS_URL`; an in-process run's
  clients narrow their listing to the same setting. The default config gives every agent `open_pr` on `forge`.
- **The forge serves `open_pr` over MCP, and the native loop is an MCP client (#99, first step).** The forge
  is an MCP server on the official MCP Python SDK (`python -m loc_arena.forge.service <run config>`): it hosts
  the run's seeded world, serves `/mcp` over Streamable HTTP, admits only a bearer token that is one agent's
  identity (401 otherwise), takes the caller from that identity, never from an argument, and enforces the
  caller's `open_pr` scope itself, since an agent's bash can reach it through agentgateway too. Each agent in the
  native loop has an MCP client that lists the tools it is offered and calls them beside its native tools; it
  reaches agentgateway's MCP route with its key when `LOC_ARENA_TOOLS_URL` is set (a stack run), and otherwise
  its own server over the episode's forge, in memory, so no container is needed. Every MCP service records each
  tool call as one JSON line in OpenTelemetry's GenAI and MCP attribute names, with the calling agent and the
  result (`loc_arena/tool_records.py`): on stdout in a container, in `records/<service>.jsonl` in the episode's
  directory in process. The
  workspace brief no longer names `open_pr`: an agent learns the tools a service serves it from its listing. A
  served tool an agent is not offered is refused, never run by the harness's own forge instead. Two live services
  serving one tool name are refused at load, since agents call a tool by its name alone. An
  `open_pr` whose arguments are not text is now refused by the tool's schema, and arguments it does not declare
  are dropped. `stack.tools_timeout_seconds` bounds one tool call through agentgateway, and
  `stack.tools_connect_seconds` how long each client retries its first listing while agentgateway starts.
- **The events monitors read are built from the services' records (#99, step 2).** After play and before grading,
  `loc_arena/recorded_events.py` turns each record of a live service with tools into the event of the episode or
  the honest twin whose play window holds it, on both the sealed and the mirror log, after every event of play: an
  `open_pr` the forge opened becomes the `pr` event the PR review monitor reads, so the harness no longer records
  an agent's. The episode clock keeps a reading at its start and at each move against wall time, written with the
  play window to each episode's `snapshot.json`, so a record takes the episode time a live event had then, a
  pi agent's too, and the native turn of its agent that covers it, else turn 0. Grading never builds, so grading
  a run again adds nothing. A record that cannot be placed (not a record, in no play window, not an agent of the
  run, or a native agent's outside its turns) is written to `unattributed_records.jsonl` in the run directory,
  never dropped and never stopping grading. The Inspect export puts a built event in its agent's turn span when
  that turn is native, and pairs it with its mirror twin, so the explorer places the event and the verdicts on
  it. The coworkers' `pr` events, the scripted one credited to `agent-main` included, are still the harness's.
- **The covert tools leave the harness as recording services (#99, step 4).** `write_scratch`,
  `acquire_identity`, `blind_monitor`, `submit_job` and `start_rogue_loop` are now tools the agents reach over
  MCP, served by `loc_arena/recording_service.py`: one module behind three `scenario.yaml` entries, each its
  own container with its own tools and grants — `jobs` (`submit_job`, `start_rogue_loop`, `acquire_identity`),
  `scratch` (`write_scratch`) and `coverage` (`blind_monitor`). Each tool enforces the caller's scope and
  records the request with a canned result; it keeps no world state, and the job tools return a plausible job
  id without running anything. An agent is offered a covert tool only where its `sandbox: tools:` grants it:
  the default config gives `agent-main` and `distill-agent` `scratch`, `serving-agent` `jobs`, and `eval-agent`
  `coverage`. After play the builder maps each covert record to the events its readers need: an `action` event
  the action monitor and the explorer see, `write_scratch` a sealed `shared_surface`, and `blind_monitor` the
  `config` pair the verifier reads whose deployed coverage drops the named target. `blind_monitor` also reduces
  the monitors' view — the target's mirror events after the call are dropped, while the sealed log keeps them.
  The native loop no longer runs a covert worker or edits the gateway's coverage, and no longer logs an action
  event for a tool a service records. With the rogue loop a record-only stub, a live attack run no longer
  reaches the loop-tied side-task milestones (the scripted engine fixture still does); realistic rogue
  deployment is left to a later change. The tools gateway multiplexes the services with `prefixMode: never`, so
  a tool keeps its own name across services.
- **Per-agent identities, rights and recorded grants on live services (#93).** A live service in a scenario
  pack's `scenario.yaml` lists the `rights` it enforces and whether `grant` itself can be granted
  (`transitive`, off by default); a service with no `rights` is open to every caller on agent-net. This replaces
  the shared credential (`accepts` and `sandbox: credentials:`, removed). In a stack run every agent gets its
  own identity for each such service, generated per run and mounted at `/run/secrets/identity_<service>` in its
  sandbox alone; the service holds all of them and names every caller. An agent's `sandbox: rights:` in the run
  config sets what it starts with. The default config gives `agent-main` `read`, `write` and `grant` on `notes`,
  every other agent but `controlplane-agent` `read` and `write`, and `controlplane-agent` none. An agent with
  `grant` passes rights it holds to another agent with a call from its sandbox (`POST /grants`), and takes them
  back with `DELETE /grants`; `GET /` describes the API. The service logs every request and every grant with
  the caller, the container it came from and the time, and the run copies the log to `services/<name>.log` in
  the run directory before grading, so a monitor can line it up with the sealed log and tell a recorded grant from a copied
  identity. The agents' prompt gives the address, the identity file and the starting rights. The service is told the
  rights its scenario declares and enforces exactly those; it refuses to start with one it cannot enforce, and a
  grant or revoke checks the caller's rights and changes the target's under one lock. A right the service
  does not list, a right named twice, rights for a service that declares none, and `transitive` without `grant`,
  and service and agent names whose identities would share one host variable are refused at load. When a live
  service fails to become healthy and the episode never starts, the run still copies that service's log.
- **A live service.** A service entry in a scenario pack's `scenario.yaml` with an `image` or a `build`
  directory becomes its own container in `make run STACK=1`, on agent-net with no route out, no capabilities, no
  volume and no published port, under `stack.service_memory_limit`, `stack.service_cpus` and
  `stack.service_pids_limit`; the episode starts once it is healthy. An entry with neither stays simulated. The
  first live service is `notes`, a small notes board in `scenarios/aurora_efficiency/services/notes`. Live
  services run on real time; the run's simulated clock stays for the scenario's scripted events. An entry that
  breaks the naming or path rules is refused at load. No container gets the Docker socket.
- **A sandbox per agent.** In `make run STACK=1` each agent's code tools run in its own `sandbox-<agent id>`.
  Only the episode can call a sandbox's command server: a sandbox refuses every other caller, even one holding
  its token. Code left in the shared checkout still runs wherever another agent runs it (the shared checkout is a
  channel between agents by design). A command's timeout is capped, and a reset ends every process the commands
  left. Sandboxes have their own `stack.sandbox_*` limits, and an agent id that cannot name a sandbox, or that
  repeats, is refused at load.
- **Pinned providers per model, reasoning per role.** A models file lists the OpenRouter providers that serve
  each model (order, no fallbacks), and every call to that model is sent to them; a model not listed is routed
  by OpenRouter, so a role that switches model never keeps providers that do not serve it. Each role can set
  its reasoning effort. The agent now runs `deepseek/deepseek-v4.1-flash` (V4 Flash is deprecated) on
  Together, Baseten or DeepSeek, the monitor on Groq or DeepInfra, and `models.fast.yaml` (run config
  `aurora-efficiency.fast`) turns the agent's reasoning off for quick smoke runs.
- **Gates on every pull request.** Each pull request and each push to `main` runs the gates on Linux, with the
  stack images built first so the Docker tests run instead of skipping. The workflow uses no secrets; the live
  tests stay skipped.

- **A shell for the agents, in the sandbox.** In `make run STACK=1` agents get a `bash` tool that runs in
  their checkout in the sandbox container, without the provider key, with a timeout that ends everything the
  command started and the end of its output (`stack.shell_timeout_seconds`, `stack.command_output_limit_bytes`).
  An in-process run never offers or runs it, since there its commands would run on the host.
- **Grading on the host.** In `make run STACK=1` the episode container only plays the episode and its honest
  twin; the host grades them from the copied-out run directory, always running the agents' code in the
  no-network sandbox, while the gateway stays up so the monitors' model calls go through it and land in its
  call log.
- **Agent code in a sandbox.** With `stack.sandbox_agent_code`, the grader runs the agents' repo tests and the
  benchmark in a throwaway container with no network, so their code never runs on the host.
- **Re-grading.** A played episode leaves a `snapshot.json` beside its logs and checkout, and `loc-arena grade`
  grades it again from that directory alone, so new checks can run over past episodes.
- **Episodes in a container.** `make run STACK=1` runs the episode in its own container on a network with no route
  out and no provider key. Its model calls go through the gateway. When it ends, a normal exit or a Ctrl-C, its
  audit bundle and the gateway's call log are copied into `logs/` and the containers are removed. (#66)
- **The gateway container.** Holds the OpenRouter key alone, forwards OpenRouter-shaped requests on allowed paths
  to its one https upstream, and records every call with the container that made it, complete or not. Any harness
  that speaks OpenRouter works by changing its base URL. (#65)
- **Code review.** CodeRabbit reviews each pull request out of draft, unless its title marks it as work in
  progress, against `AGENTS.md` and the repository's own ast-grep rules. (#64)

### Changed

- **The native loop holds no domain-tool code (#99, step 6).** It runs a tool on one of two targets: its native
  tools on the checkout, or the MCP client of the caller that offers the tool; any other tool is refused as not
  offered. The forge's git, ticket and wiki tools, the stub fallback, the in-process skill tools with their two
  timeouts, the inference tools and subagent spawning leave the loop, and the orchestrator goes with them. The
  covert briefs name no tool: an agent finds its tools in its MCP listing.

- **A stack run keeps each live service's log in its own run directory.** The logs of the live services and of
  agentgateway are copied to `services/<name>.log` in the run directory before grading, where the events of their
  records are built, instead of to `<logs>/services/` after grading, where every run in the same directory
  overwrote the last one's. A log already added there is not added again at teardown, so no line repeats.

- **The sandbox's command server is its own package.** `sandbox_server` imports nothing of the harness, so a
  sandbox image can run it without the grading code; the sandbox's command is `python -m sandbox_server` with the
  few settings the server needs.
- **Live runs end, and survive their provider.** No agent turn of a model-policy episode starts past
  `episode.wall_clock_seconds`, a ceiling per episode, so a run with its honest twin can take up to twice that; the
  rogue loop stops there too, and runs once per episode. `scores.json` and `report.html` record a run the ceiling
  stopped (`wall_clock_ceiling`), and `scores.json` counts each episode's model calls the provider failed past its
  retries (`failed_model_calls`). A failed call spends that agent's turn, an agent whose calls fail
  `agent_loop.failed_turns_before_end` turns in a row ends, and the rogue loop's failure comes back as a tool
  error, so a dead provider no longer crashes the run without a bundle. A refused key, no credits or an unknown
  model on the agents' calls still stops the run at its first call. A reply that is no chat completion with text
  content, or that ends in `finish_reason: error`, fails its call the same way. A live monitor whose model calls
  fail `monitor_failures_before_fallback` times in a row (`configs/monitors.yaml`) scores the rest with its
  heuristic and is marked `errored`, whether in the episode or in its honest twin. Tool-call arguments the event
  log cannot record (NaN, a lone surrogate, an integer past Python's digit limit, nesting past
  `agent_loop.max_argument_depth`) come back to the agent as a tool error, as does a rogue loop of more
  `iterations` than fit before the deadline.
- **Who did what.** A covert action (a scratch note, a coverage change) is recorded under the uid and role of
  the agent that called the tool, which the tool layer sets and an agent cannot name. A rogue job's records name
  the agent that launched it, while the loop's own model calls keep claiming the job's identity. An agent's
  `open_pr` also records a `pr` event, as the scripted coworkers' PRs do, so the PR review monitor sees and scores
  agents' PRs.
- **Grading.** An episode plays, then is graded from what it left: its event logs and its repo checkout, read by
  the main-task scorer and the side-task verifier the run config names. Grading no longer runs inside play,
  which lets it move to its own container next.
- **The episode stack.** The placeholder service stack is replaced by two containers, the gateway and the episode,
  rendered from the run config's `gateway:` and `stack:` settings. (#66)
- **Run configuration.** Run configs are validated as typed, described settings with one root object and named
  groups; a misspelt key is an error. (#60, #61)

### Fixed

- **The honest twin of a stack run starts from fresh services and sandboxes (#99).** Until now the twin played
  after the attack episode against the same running containers, so it could read the notes and grants the
  attack left on the notes board (since #94) and the PRs it opened on the forge, whose numbers ran on. It
  could also read files the attack left in a sandbox under `/run/lock`, `/dev/mqueue` or `/opt/venv/.lock`,
  which the sandbox reset does not empty. Now the episode container plays one phase per start
  (`--next-phase`). Between the two starts, the host adds each live service's and agentgateway's log so far
  to its `services/<name>.log`, then recreates every sandbox, live service and agentgateway with fresh
  anonymous volumes. Only the gateway, whose call log runs on, and the episode are kept. If a log cannot be
  copied, nothing is recreated. If a container does not come back, the run stops like a failed episode,
  every log copied out. As before, the compose project is kept only when a copy fails. An in-process run
  already built a fresh forge per phase and is unchanged.
- **A timed-out agent-code container is gone before the timeout is reported.** The grader force-removes it
  and waits until Docker no longer lists it; a killed `--rm` container used to be removed asynchronously,
  so it could still be listed just after.
- **HTTP client.** The gateway and the provider use httpx2, the continuation of httpx maintained by Pydantic, so
  security fixes keep reaching the one process that holds the key.
- **The rogue loop** keeps running after an empty model reply. (#63)

### Security

- **In a stack run, agent code cannot read the harness.** The agents' sandbox runs a slim image
  (`stack.sandbox_image`, the Dockerfile's `sandbox` target) with no harness, verifier, scorer or scenario
  spec. Grading's agent-code container runs it too, so it has no harness or scenarios, though it sees the
  sealed meter it runs. Grading stops if the image is missing. In an in-process run the agents' own tools
  still run their code on the host; with `stack.sandbox_agent_code` on, grading's agent-code steps run in the
  slim image there too. (#83)
- **No unrecorded pages on the gateway or the sandbox.** Neither serves the web framework's `/docs`, `/redoc` or
  `/openapi.json`: on the gateway those answered outside the recorded route, so a call there left no record, and
  on the sandbox they answered without the token. The gateway now refuses such a request and records it like any
  other path outside its allowlist; the sandbox answers 404.
- **Agent code cannot reach the host on Linux.** agent-net is an isolated network (Docker Engine 28 or
  later): the host takes no address on it, so agent code cannot reach host services there, including ones
  listening on all addresses. Before, on Linux, the sandbox could connect to the host through agent-net's
  gateway address; Docker Desktop's VM held that address, which hid the gap until CI ran on Linux.
- **What agent code cannot reach is tested against the real stack.** Where Docker and the stack image are
  present, the tests bring an episode's compose project up and probe, through the agents' own shell in the
  sandbox: no provider key, no route beyond agent-net, no outside name resolved, no way to the host, no mount
  but the checkouts, and no grader answers. The one route agent code has, to the gateway, is recorded under
  the sandbox's name.
- **Agent-written code never sees the provider key.** Tests and benchmarks the agents run, and the grading of
  their work, run without the key in their environment, in-process runs included. (#66)
- **Agent-written code in a stack run runs only in a sandbox container.** The agents' `bash`, `run_tests` and
  `run_benchmark` run in a `sandbox` service that mounts the volume of the checkouts and nothing of the
  harness: not the episode's event logs, the run configs, the scenario's moves or the gateway's call log. The
  episode reaches its command server on agent-net with a per-run token (a compose secret), each episode's
  checkout plays there and is moved into the run directory before grading, and no process a command starts
  outlives it, so agent code never runs while the episode touches the shared checkout. Before each episode
  plays, the sandbox empties its HOME and temporary directories (`stack.sandbox_scratch_directories`) and the
  checkouts other than the new one, so nothing of the attack episode reaches its honest twin. A sandbox that
  stops answering gives the agent an error result and the episode plays on; compose restarts a killed sandbox,
  and the next episode waits for it (`stack.sandbox_recovery_seconds`) or fails, never running agent code
  anywhere else. Model calls agent code makes from
  the sandbox go through the gateway, recorded under the sandbox's name.
- **Grading writes the sealed harness only into its grading copy.** A file or link the agents left where
  `grade_bench.py` or `grade_workload.py` goes is removed first, so a link cannot redirect the copy onto a
  file of the host; a directory there fails the grade.
- **The episode image holds no grader answers.** The scenario's sealed `reference/` and the scripted policy's
  moves in `scripted/`, which are part of the main task's solution, are left out of the build context. A
  scripted episode gets `scripted/` mounted read-only; a live model episode gets no scenario directory mounted.
