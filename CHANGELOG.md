# Changelog

What each part of LOC-Arena does for a run, and how that changed. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Entries describe systems and what they now do; the commit
log has the details.

## [Unreleased]

### Added

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

- **Agent-written code never sees the provider key.** Tests and benchmarks the agents run, and the grading of
  their work, run without the key in their environment, in-process runs included. (#66)
