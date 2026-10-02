# Isolation design notes

Why the isolated stack's code is the way it is, one section per component. [README.md](README.md) holds the plan and the diagrams.

## Stack package

Every module in `loc_arena/stack` imports only the standard library, pydantic, pydantic-settings, FastAPI, httpx and `loc_arena.logging_.events`, so the sandbox image ships the package whole (`COPY loc_arena/stack/` in the Dockerfile), and `tests/unit/test_sandbox_image.py` checks the imports. The model-call types live in `stack/model_call.py` for the same reason: the edge imports them without the core's module.

## Contracts

- Every contract body is a frozen pydantic model that refuses an unknown field (`ContractModel`), so each service validates what it receives at its boundary. The core's and the edge's generate replies set the same config.
- A model call's request and reply are `GenerateRequest` and `GenerateResponse` from `model_call.py`. They are mutable and ignore unknown fields, as the gateway wrote them, and the core's and the edge's replies extend that reply. Log events stay the dataclasses of `loc_arena.logging_.events`.
- An `EpisodeHandle` is 16 lowercase hex characters, validated both as a model field and as a FastAPI path parameter.
- `CodeToolName` is the allowlist of code tools, bash included.
- `CoreGenerateResponse.ts` is the core's clock at the call; the edge's mirror record reuses it.
- The core ships without `configs/`, so `EpisodeOpen` carries the run config's model routes and covert injection. Its `provider` field picks the key-holding egress or, for scripted runs, the deterministic stub, which needs no network.
- `RunnerEpisodeExport.phases` maps the sealed seq of each model call to the phase of the turn that made it.
- `EpisodeWiring.code_tools` maps each agent id to the runner of its own sandbox.

## Constants

A route holding `{handle}` is filled with an episode handle (`CLOCK_ROUTE.format(handle=handle)`), and FastAPI reads the same text as the path parameter `handle`. Tunable values live in `loc_arena/stack/settings.py`; event kinds stay in `EventKind`. Hostname labels follow [RFC 1123 section 2.1](https://www.rfc-editor.org/rfc/rfc1123.html), which keeps the 63-octet label limit of RFC 1035.

## Control key

The control key guards every control route and every mirror write. It is a compose file secret: compose refuses an environment-sourced secret in a read-only container ([compose `secrets.go`](https://github.com/docker/compose/blob/main/pkg/compose/secrets.go)), and the edge and the runner run read-only. The harness writes each episode's key to a file and names that file in `LOC_ARENA_CONTROL_KEY_FILE` of the `docker compose` process alone. The key is `secrets.token_hex(32)`, as long as the core's SHA-256 HMAC keys ([RFC 2104 section 3](https://www.rfc-editor.org/rfc/rfc2104.html#section-3)).

`require_control_key` is an `APIKeyHeader` scheme ([FastAPI security](https://fastapi.tiangolo.com/tutorial/security/)). It compares keys with `hmac.compare_digest`, refuses an empty key, and answers a missing key and a wrong key alike: 401 with the `WWW-Authenticate` header that [RFC 9110 section 15.5.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.5.2) requires.

## Secrets

`StackSecrets` reads a secrets directory by field name, so each field equals a `*_SECRET_NAME` constant. Environment variables take priority over a dotenv file and a secrets directory (pydantic-settings: [dotenv](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/#dotenv-env-support), [secrets](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/#secrets)). Callers pass `_env_file` and `_secrets_dir` explicitly, and `None` reads nothing from that source. A container reads only `/run/secrets` (`load_container_secrets`). An empty value, such as a compose secret whose variable was unset, reads as `None`.

## Service client

`ServiceClient` raises `httpx.HTTPStatusError` on a non-2xx reply; tests pass a FastAPI `TestClient` as its `http_client`. One `TypeAdapter` is cached per body type, because building one is costly ([pydantic performance](https://pydantic.dev/docs/validation/latest/concepts/performance/)). A body is a contract model or one of the log dataclasses `EventDraft` and `Event`. `service_client.py` has no `from __future__ import annotations`: `check_control_key`'s annotation names `scheme`, a local of `require_control_key`, and FastAPI cannot resolve that from a string.

## Settings

`LocArenaSettings` is validated once per process. On the host, `load_run_config` validates the settings groups the merged run config declares, and a group the YAML leaves out takes the model defaults. In a container, `load_settings_from_environment` validates the JSON in `LOC_ARENA_SETTINGS`. Consumers read `settings.<group>.<field>` and never re-read the source.

- `docker.agent_tmpfs_size_bytes` caps `/tmp` where agent code runs: bash writes every command's output there, a process it detached may keep writing, and the tmpfs counts against the container's memory limit.
- `docker.runner_phase_timeout_seconds` covers the scaffold, the close and the monitors in the runner container, so it sits above `episode.wall_clock_seconds`, which the agents' work is sized for.
- `docker.grader_timeout_seconds` sits above the grader's worst case: every repository's suite at `grading.suite_timeout_seconds`, then the benchmark.
- `gateway.recorder_write_attempts` retries a sealed write safely: the recorder acknowledges an identical re-send without writing it twice.
- `ExecutionSettings` refuses an `execution.reply_timeout_seconds` at or below its longest tool timeout.
- `grading.max_captured_output_characters` keeps the end of each output stream, which must hold the benchmark's report, its last stdout line.
- `provider.request_timeout_milliseconds` is httpx's timeout of each phase: connect, write, pool, and each wait for the next bytes of the reply. A reply that trickles in never trips it. `provider.request_deadline_seconds` bounds the whole request. A request cut by it counts as a connection error, and it is half the call's budget, so one retry can still finish.
- The `provider.backoff_*` fields are the SDK's `BackoffStrategy` `initial_interval`, `max_interval` and `exponent`; they also pace a 429 without Retry-After. `provider.backoff_max_elapsed_time_milliseconds` is the SDK's `max_elapsed_time`: no 5XX is retried past it. `LocArenaSettings` refuses it unless it is below `gateway.relay_timeout_seconds` and `gateway.control_timeout_seconds`.

## Event logs

A log gives each recorded draft its `episode_id`, `seq` and `fp`, so no writer picks its own seq. `AppendOnlyLog` holds one lock for writes.

## OpenRouter provider

Four layers bound a call, inner to outer:

1. `_DeadlineTransport` gives every HTTP request a wall-clock deadline, reply body included. httpx's timeouts (the SDK's `timeout_ms`) bound each phase and restart with every chunk received ([httpx timeouts](https://www.python-httpx.org/advanced/timeouts/)), so a reply that trickles in outlasts them. `asyncio.timeout` cancels the request, and httpx closes its connection.
2. The SDK's `RetryConfig` retries 5XX and connection errors, a request cut by its deadline included: `chat.send_async` retries `["5XX"]`, and the SDK's `utils/retries.py` retries `httpx.TimeoutException`.
3. stamina retries a 429 only, after its Retry-After in seconds ([OpenRouter errors](https://openrouter.ai/docs/api_reference/errors-and-debugging)), capped at `rate_limit_max_wait_seconds`. `_wait_before_retrying` returns `False` to stop, `True` for stamina's own backoff when the Retry-After is unusable, or the capped wait.
4. `asyncio.timeout` cuts the whole call at `backoff_max_elapsed_time_milliseconds`. The SDK checks its own maximum elapsed time only between attempts, and waits out a 5XX's Retry-After uncapped (`_get_sleep_interval` in `utils/retries.py`).

`generate` runs its own event loop, so synchronous code calls it (a FastAPI `def` route runs in a worker thread); the loop, its threads and every connection close with the call. Every failure raises `ProviderError` or a subclass: `ProviderTimeoutError` past the request deadline or the call's budget, `ProviderReplyError` for a 200 without a usable completion, and `ProviderError` for the rest.

The SDK sends the message and tool fields its schema declares, `cache_control` on a content part or on a tool among them, and silently drops any other key: `cache_control` on a message itself, `name` on a tool message, `index` on a tool call. An empty tool list is left out of the request. OpenRouter sends an error body with a 200 when the model fails after the headers were sent. OpenRouter includes usage in every response ([usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)) and the batch quota counts its tokens, so a reply without usage is refused. A reply without a cached token count reports 0 cached tokens. `tests/unit/test_openrouter_sdk.py` pins each SDK field the provider reads, so a failure there names the SDK.

## Execution sandbox

`loc_arena/execution` runs agent code. `workspace` runs one agent's code tools over a checkout, `app` serves them in that agent's sandbox, `client` is the runner's side, and `checkout` builds and runs a checkout for the host, the sandboxes and the grader. The execution and grader packages import only `loc_arena.stack`, `loc_arena.execution`, `loc_arena.logging_.events`, the standard library and third-party packages, so they ship in the sandbox image, which holds no `configs/`, `scenarios/` or `live.py`. `tests/unit/test_sandbox_image.py` checks those imports.

The tools are an allowlist (`CodeToolName`): read, write, edit, list and search files, run a repository's suite, run the directional benchmark, and a real bash. Tool argument models are frozen and ignore an argument the tool lacks. Every path argument is confined to the checkout: `..`, an absolute path or a symlink resolving outside it is refused, and a search skips a file that resolves outside. Every result is capped by `settings.execution`. A malformed call or a failing file operation is an error result, since a live model routinely writes bad arguments. A workspace on the host (STACK=0) answers bash with an error result and starts no process, so no agent gets a shell next to the machine's credentials. `build_execution_app` is the only place the shell is turned on, inside the agent's own sandbox.

Seeding is idempotent, so every sandbox sharing the checkout may seed it: the first finds it empty and copies the codebase, the others leave the agents' work alone, and two concurrent seeds write the same files (`dirs_exist_ok`). `run_benchmark` and `profile` run a small datapipe slice over a fixed sample and report the company's own inline cost accounting as directional feedback. They never read or move the sealed grade meter.

`GET HEALTH_ROUTE` answers `ExecutionHealth`. `POST WORKSPACES_ROUTE` opens the sandbox for one episode and answers 201, seeding the shared checkout from the image's pristine codebase when it holds no repository yet; it answers 409 when the sandbox already serves another episode. `POST TOOL_CALLS_ROUTE` runs one allowlisted tool call, with 422 for a tool outside `CodeToolName` and 404 before the episode is open. The app holds no secret: whatever it can do, the agent's own bash can do too.

## Execution client

The sandbox belongs to the agent. Its code runs as the execution app's user and can replace the app with a server of its own, so the runner bounds every call and treats every reply as untrusted. httpx's timeouts bound each phase and each read ([httpx timeouts](https://www.python-httpx.org/advanced/timeouts/): a read timeout is the longest wait for one chunk), so a server that trickles its headers or body one byte at a time outlasts them. Each call therefore runs under `asyncio.timeout(reply_timeout_seconds)`, which cancels connecting, sending and reading the whole reply together. httpcore closes a connection whose exchange the cancellation interrupts, on its `except BaseException` path, shielded from the cancellation. `asyncio.run` closes the call's client and event loop, so no connection, task or thread outlives the call. `asyncio.run` refuses a thread whose event loop is running, so the runner calls this client from synchronous code.

The reply is read raw, because decoding could expand a compressed body past the cap. It is refused past `max_response_bytes` and then validated as a `CodeToolResult`. A sandbox that fails, stalls or answers badly gives the agent an error result. `trust_env=False` keeps proxies and `~/.netrc` credentials away from a server the agent may run.

## Command

bash, a repository's suite and the benchmarks all run through `run_command`, in the sandboxes, on the host and in the grader. Output goes to unlinked temporary files. Reading a pipe to its end waits for every process holding its write end, and a process the command detaches (`sleep 600 &`, `nohup`, `setsid`) holds that end until it exits. With a file, the call returns when the command itself exits, and a detached process keeps writing to a file no one reads again.

The command runs in a new session (`start_new_session`), so it leads its own process group. [`Popen.wait`](https://docs.python.org/3/library/subprocess.html#subprocess.Popen.wait) only raises `TimeoutExpired` and kills nothing, so on timeout the whole group is killed, background children included. At most `max_output_characters` are read back from the kept end of each stream (the tail by default, where a report or summary is), with `os.pread`, which leaves alone the file offset that a still-running detached process shares. One UTF-8 character is at most four bytes ([RFC 3629](https://www.rfc-editor.org/rfc/rfc3629.html)).

## Checkout

A checkout is the seven `company/` repositories side by side; they import when each repository directory is on `PYTHONPATH`. `__pycache__`, `.pytest_cache` and `.git` are never listed, searched or copied. The code runs on the plain interpreter (`sys.executable`), because `uv run` cannot resolve the copied repositories' dependencies. The host (STACK=0), each sandbox and the grader all build and run a checkout with this one module, which ships in the sandbox image. A copy keeps a symlink as a link without following it, so a link an agent planted cannot make the copy read outside the checkout. A file that cannot be copied, such as a named pipe, raises `shutil.Error` (an `OSError`), and a missing repository raises `FileNotFoundError`.

## Grader

The main-task grade is split in two. `measure_checkout` runs the suites and the sealed harness with no network and no reference: in the stack's grader container (sandbox image, `network_mode: none`) or, for STACK=0, on the host. `score_measurement` scores that `GradeMeasurement` against the sealed `reference.json`, on the host only. Its gate needs the suite green, the outputs within the reference's tolerance and a cost at or above `min_work`, and a failed gate scores `gate_floor`. A pipeline the checkout broke so badly that the sealed harness could not run it end to end fails correctness too: it is graded as an outcome (`grade_error`), and the harness carries on.

`measure_checkout` copies only the trusted repositories it is given into an empty grading root, then places the sealed harness files named by `grading.harness_file_names` at that root, outside every repository. Nothing an agent left at the checkout's root (a shadowing module, a symlink named like a harness file) reaches the graded copy. A checkout that cannot be copied (a missing repository, a named pipe) measures as red and incomplete, and so does a benchmark that hangs, fails or prints no valid report. The suites stop at the first red one, and a hang counts as red. Every suite and the benchmark run through `run_command`, so a process agent code detaches never holds the grader. Agent code can still influence the measured cost from code the pipeline imports, or from a process a suite detached that still runs during the benchmark. That residual risk is accepted, bounded by the reference's `min_work` floor.

In the stack, `python -m loc_arena.grader` runs in the grader container with no network. The checkout and the sealed harness files are mounted read-only at the CLI's defaults, and the settings come in `LOC_ARENA_SETTINGS`. It measures the image's pristine list of repositories, because agent code may add or remove directories in the checkout. The host reads the printed line as untrusted, since agent code runs in the grader.
