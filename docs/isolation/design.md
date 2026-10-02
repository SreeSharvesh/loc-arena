# Isolation design notes

Why the isolated stack's code is the way it is, one section per component. [README.md](README.md) holds the plan and the diagrams.

## Stack package

Every module in `loc_arena/stack` imports only the standard library, pydantic, pydantic-settings, FastAPI, httpx and `loc_arena.logging_.events`, so the sandbox image can ship the package whole. The model-call types live in `stack/model_call.py` for the same reason: the edge imports them without the core's module.

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
