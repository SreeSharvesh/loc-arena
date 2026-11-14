# What a run cost

Every `make run` writes `cost.json` in its run directory, beside `scores.json`, and prints one line of it in
its summary. It is the engine's profiler: what a decision costs is found by running it and comparing two
runs. No score, event, monitor or verifier reads the file, and `scores.json` keeps its keys.

```sh
make cost RUN_DIR=logs/<slug>/<run>                          # what one run cost
make cost RUN_DIR=logs/<slug>/<run> OTHER=logs/<slug>/<run>  # the second run's cost, and how it differs
```

## What the file holds

| Key | What it is |
|---|---|
| `wall_seconds`, `phases` | the whole run, and its phases: `stack_start` (images built, containers created, the model gateway started), `services_start` (the services started until all are healthy and the episode starts; absent when no sample saw the episode, its time then in `play`), `play` (both plays, the renewal between them, the company's server deployed before each, the runners stopped and Phoenix's spans exported after), `copy` (the records copied out and their events built), `grading`, `teardown` |
| `containers` | per container: `memory_rest_bytes` (as the episode starts; none for one that was not running then), `memory_peak_bytes`, `memory_limit_bytes` (none: no limit), `cpu_seconds` |
| `containers_rest_bytes`, `containers_peak_bytes` | what every container held together, as the episode starts and at the highest sample |
| `host_memory_peak_bytes` | the most memory in use on the machine the run's CLI is on |
| `images`, `volumes`, `records` | the size of each image the run's containers use, of each volume of the project once grading ends (an anonymous one as `service:path`), and of each entry of the run directory |
| `dollars` | the total, `by_model` and `by_caller`, from the cost each answered call carries in the gateway's record |
| `sampler` | its interval, how many samples it took, how many failed, and the last error |

A container is named by its compose service, so a service the honest twin's renewal recreated is one
container: its peak is the higher of the two and its CPU seconds are their sum. The containers the agents'
code is graded in are one container, `agent-code`. A caller is the agent whose loop made the call, else who
launched the workload that made it, else the gateway's name for it; the monitors' calls are `host`'s.

## How it is measured

The run's host process samples the Docker daemon on a thread, every `cost.sample_seconds`, for the containers
of the run's own compose project; nothing an agent reaches holds the Docker socket. It reaches the daemon
`DOCKER_HOST` names, else the default socket; it does not read docker contexts. Memory is the daemon's usage
less the inactive file cache, as `docker stats` shows it. CPU seconds are the daemon's cumulative counter at a
container's last sample. Memory at rest is taken at the first sample in which the episode container runs:
it starts once every service it waits for is healthy, so the figure is within one interval of that start, as
the agents' sessions begin.
`loc_arena.run_cost.mark_phase` marks where play and grading start and end.

What it cannot see:

- **A spike shorter than the interval.** A peak is the highest sample. The Docker Engine API has no peak of
  its own on cgroup v2. For the same reason the end of `services_start` is known to within one interval, a
  container's CPU after its last sample is not counted, and a graded container shorter than the interval
  can be missed.
- **A run that failed before grading** writes no file: it has no run directory.
- **Two runs grading at once on one machine** each count the other's `agent-code` containers, which carry
  no project.
- **Where containers run in a virtual machine** (macOS, Windows), `host_memory_peak_bytes` is the computer's,
  not the virtual machine's.

A sample that fails is counted in `sampler.failed_samples`, with the last error, and the run goes on; a
cost file that cannot be written is said in the summary, and the run's records and scores stand.

## Settings (`cost:`)

| Key | Default | What it sets |
|---|---|---|
| `sample_seconds` | 2.0 | the wait between two samples |
| `request_seconds` | 10.0 | the longest one call to the daemon may take |

## Sources

- Docker Engine API, [specification](https://github.com/moby/moby/blob/master/api/docs/v1.56.yaml):
  `GET /containers/{id}/stats` with `one-shot`, its memory and CPU fields, and that `max_usage` is not set on
  cgroup v2; `GET /system/df` and a volume's `UsageData`.
- Docker CLI, [`container stats`](https://github.com/docker/cli/blob/master/docs/reference/commandline/container_stats.md):
  the memory it shows is usage less the inactive file cache.
- OpenRouter, [usage accounting](https://openrouter.ai/docs/use-cases/usage-accounting.md): every response
  carries `usage.cost`, a stream's in its last chunk.
