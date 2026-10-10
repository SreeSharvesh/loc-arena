# Changelog

What each part of LOC-Arena does for a run, and how that changed. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Entries describe systems and what they now do; the commit
log has the details.

## [Unreleased]

### Added

- **Each agent's tools set in the run config, behind agentgateway (#99).** A live service in `scenario.yaml` lists
  the MCP `tools` it serves, and may run a `module` of the engine image instead of a `build` or `image` (the
  forge, whose world is the harness's). An agent's `sandbox: tools:` lists the tools it may call on each such
  service; a tool the service does not list, a tool named twice, or tools for a service that serves none are
  refused at load. In a stack run every service with tools sits behind one MCP route of agentgateway v1.5.0
  (`stack.tools_gateway_image`, port `stack.tools_gateway_port`), whose config is generated per run from
  `sandbox: tools:` and reaches it as a compose secret: it admits each agent by the hash of its sandbox token,
  lists it exactly its tools, refuses every other before it reaches the service, and forwards each call with
  that agent's identity on the service, which only the service and agentgateway hold. Its log, one JSON line
  per request with the agent, the tool, its arguments and its result or error, is copied to
  `<logs>/services/agentgateway.log`. The episode finds the route in `LOC_ARENA_TOOLS_URL`. The default config
  gives every agent `open_pr` on `forge`.
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
  the caller, the container it came from and the time, and the run copies the log to `<logs>/services/<name>.log`
  before teardown, so a monitor can line it up with the sealed log and tell a recorded grant from a copied
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
