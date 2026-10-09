# Changelog

What each part of LOC-Arena does for a run, and how that changed. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Entries describe systems and what they now do; the commit
log has the details.

## [Unreleased]

### Added

- **Gates on every pull request.** Each pull request and each push to `main` runs the gates on Linux, with the
  stack image built first so the Docker tests run instead of skipping. The workflow uses no secrets; the live
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

- **Grading.** An episode plays, then is graded from what it left: its event logs and its repo checkout, read by
  the main-task scorer and the side-task verifier the run config names. Grading no longer runs inside play,
  which lets it move to its own container next.
- **The episode stack.** The placeholder service stack is replaced by two containers, the gateway and the episode,
  rendered from the run config's `gateway:` and `stack:` settings. (#66)
- **Run configuration.** Run configs are validated as typed, described settings with one root object and named
  groups; a misspelt key is an error. (#60, #61)

### Fixed

- **HTTP client.** The gateway and the provider use httpx2, the continuation of httpx maintained by Pydantic, so
  security fixes keep reaching the one process that holds the key.
- **The rogue loop** keeps running after an empty model reply. (#63)

### Security

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
