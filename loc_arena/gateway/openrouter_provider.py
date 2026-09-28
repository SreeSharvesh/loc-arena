"""The OpenRouter provider: the one egress to a model, through the official SDK, bounded in time.

Four layers, inner to outer, each with one job:

1. :class:`DeadlineTransport` gives every HTTP request a wall-clock deadline, reply body included. httpx's
   timeouts (the SDK's ``timeout_ms``) bound each phase and restart with every chunk received
   (python-httpx.org/advanced/timeouts), so a reply that trickles in outlasts them. ``asyncio.timeout``
   cancels the request and httpx closes its connection.
2. The SDK's ``RetryConfig`` retries 5XX and connection errors, a request cut by its deadline included
   (``chat.send_async`` retries ``["5XX"]``; ``utils/retries.py`` retries ``httpx.TimeoutException``).
3. stamina retries a 429 only, after its Retry-After (seconds, per openrouter.ai/docs/api_reference/
   errors-and-debugging), capped.
4. ``asyncio.timeout`` cuts the whole call at ``backoff_max_elapsed_time_milliseconds``: the SDK checks its
   own max elapsed time only between attempts, and waits out a 5XX's Retry-After uncapped
   (``utils/retries.py``, ``_get_sleep_interval``).

``generate`` runs its own event loop, so it is called from synchronous code (a FastAPI ``def`` route runs in a
worker thread); the loop, its worker threads and every connection close with the call. A failure raises a
:class:`ProviderError`, never an SDK or httpx type.
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx
import stamina
from openrouter import OpenRouter, components, errors
from openrouter.utils import BackoffStrategy, RetryConfig
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from loc_arena.gateway.core import Message, ProviderResult, ToolSpec
from loc_arena.stack.settings import ProviderSettings
from loc_arena.stack.stack_secrets import StackSecrets

MILLISECONDS_PER_SECOND: Final = 1000
BACKOFF_STRATEGY: Final = "backoff"  # RetryConfig.strategy is "none" or "backoff"


class ProviderError(RuntimeError):
    """A model call failed: refused, still failing after every retry, or answered without a completion."""


class ProviderTimeoutError(ProviderError):
    """A request outlived its deadline, or the call with its retries outlived its budget."""


class ProviderReplyError(ProviderError):
    """The provider answered 200 without a usable completion: an error body, no choice, or no token usage."""


class OpenRouterErrorDetail(BaseModel):
    """The ``error`` object of an OpenRouter error body."""

    model_config = ConfigDict(frozen=True)

    code: int
    message: str


class OpenRouterErrorResponse(BaseModel):
    """An OpenRouter error body; it comes with a 200 when the model fails after the headers were sent."""

    model_config = ConfigDict(frozen=True)

    error: OpenRouterErrorDetail


class DeadlineTransport(httpx.AsyncHTTPTransport):
    """httpx's transport, except that a request and its whole reply must complete within a deadline."""

    def __init__(self, deadline_seconds: float) -> None:
        """Serve requests like ``httpx.AsyncHTTPTransport()``, each within ``deadline_seconds``."""
        super().__init__()
        self._deadline_seconds = deadline_seconds

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` and read its reply, or raise an httpx timeout at the deadline."""
        try:
            async with asyncio.timeout(self._deadline_seconds):
                response = await super().handle_async_request(request)
                await response.aread()  # the body too: a reply that trickles in must not outlast the deadline
                return response
        except TimeoutError as error:  # an httpx timeout, so the SDK retries it as a connection error
            raise httpx.TimeoutException(
                f"no complete reply within the {self._deadline_seconds} s request deadline",
                request=request,
            ) from error


class OpenRouterProvider:
    """The real egress (a ``loc_arena.gateway.core.Provider``): one chat completion per call, via the SDK."""

    def __init__(self, settings: ProviderSettings, api_key: SecretStr) -> None:
        """Keep the key masked; build the SDK's 5XX retries and stamina's 429 retries from ``settings``."""
        self._settings = settings
        self._api_key = api_key
        self._server_error_retries = RetryConfig(
            strategy=BACKOFF_STRATEGY,
            backoff=BackoffStrategy(
                initial_interval=settings.backoff_initial_interval_milliseconds,
                max_interval=settings.backoff_max_interval_milliseconds,
                exponent=settings.backoff_exponent,
                max_elapsed_time=settings.backoff_max_elapsed_time_milliseconds,
            ),
            retry_connection_errors=settings.retry_connection_errors,
        )
        # A retrying caller, not the decorator: stamina's retry log then holds no call argument (no message).
        self._rate_limit_retries = stamina.AsyncRetryingCaller(
            attempts=settings.rate_limit_attempts,
            timeout=None,  # the call's budget bounds the retries (see _complete)
            wait_initial=settings.backoff_initial_interval_milliseconds / MILLISECONDS_PER_SECOND,
            wait_max=settings.rate_limit_max_wait_seconds,
            wait_exp_base=settings.backoff_exponent,
        ).on(self._wait_before_retrying)

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        """Send ``messages``, and ``tools`` when there are any, to ``model``; return the completion and usage.

        Raises ``ProviderTimeoutError`` past the request deadline or the call's budget, ``ProviderReplyError``
        for a 200 without a usable completion, and ``ProviderError`` for any other failure.
        """
        try:
            reply = asyncio.run(self._complete(model, messages, temperature, max_tokens, tools))
        except TimeoutError as error:
            raise ProviderTimeoutError(
                f"{model}: no completion within the call's budget of "
                f"{self._settings.backoff_max_elapsed_time_milliseconds} ms",
            ) from error
        except httpx.TimeoutException as error:
            raise ProviderTimeoutError(f"{model}: {error}") from error
        except errors.ResponseValidationError as error:
            raise ProviderReplyError(describe_unusable_reply(model, error)) from error
        except ValidationError as error:  # the SDK refused to build the request; its text quotes the messages
            reason = f"the SDK cannot send these messages or tools ({error.error_count()} errors)"
            raise ProviderError(f"{model}: {reason}") from error
        except (errors.OpenRouterError, errors.NoResponseError, httpx.HTTPError) as error:
            raise ProviderError(f"{model}: {type(error).__name__}: {error}") from error
        return read_completion(model, reply)

    async def _complete(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> components.ChatResult:
        """One chat completion within the call's budget, the SDK's and stamina's retries included."""
        async with (
            asyncio.timeout(self._settings.backoff_max_elapsed_time_milliseconds / MILLISECONDS_PER_SECOND),
            # trust_env=False: an environment proxy would get httpx's own transport, without the deadline.
            httpx.AsyncClient(
                transport=DeadlineTransport(self._settings.request_deadline_seconds),
                trust_env=False,
            ) as http_client,
        ):
            sdk = OpenRouter(
                api_key=self._api_key.get_secret_value,  # the SDK keeps the getter and reads it per request
                server_url=self._settings.server_url,
                async_client=http_client,
                retry_config=self._server_error_retries,
                timeout_ms=self._settings.request_timeout_milliseconds,
            )
            return await self._rate_limit_retries(
                sdk.chat.send_async,
                model=model,
                messages=messages,
                tools=tools or None,  # an empty tool list is left out of the request, like an absent one
                temperature=temperature,
                max_completion_tokens=max_tokens,
                stream=False,
            )

    def _wait_before_retrying(self, error: Exception) -> bool | float:
        """Retry a 429 only: after its Retry-After, capped; stamina's backoff when it gives no usable one."""
        if not isinstance(error, errors.OpenRouterError) or error.status_code != HTTPStatus.TOO_MANY_REQUESTS:
            return False
        try:
            retry_after_seconds = float(error.headers.get("retry-after", ""))
        except ValueError:
            return True
        if retry_after_seconds >= 0:  # false for NaN too
            return min(retry_after_seconds, self._settings.rate_limit_max_wait_seconds)
        return True


def describe_unusable_reply(model: str, error: errors.ResponseValidationError) -> str:
    """Say why a reply is no chat completion, quoting OpenRouter's own error when the body is one."""
    try:
        detail = OpenRouterErrorResponse.model_validate_json(error.body).error
    except ValidationError:
        return f"{model} answered {error.status_code} with a body that is not a chat completion"
    return f"{model} answered {error.status_code} with error {detail.code}: {detail.message}"


def read_completion(model: str, reply: components.ChatResult) -> ProviderResult:
    """The first choice's text and the reply's token usage; ``ProviderReplyError`` when either is missing.

    OpenRouter includes usage in every response (openrouter.ai/docs/cookbook/administration/usage-accounting),
    and the batch quota counts it, so a reply without usage is refused rather than estimated. The choice's
    tool calls come back too, and the cached token count, which is optional and nullable in the SDK's usage,
    so a reply without one reports 0 cached tokens.
    """
    if not reply.choices:
        raise ProviderReplyError(f"{model} answered without a choice")
    choice = reply.choices[0]
    if choice.finish_reason == "error":
        raise ProviderReplyError(f"{model} failed while generating (finish_reason 'error')")
    if reply.usage is None:
        raise ProviderReplyError(f"{model} answered without token usage")
    content = choice.message.content
    if isinstance(content, list):
        raise ProviderReplyError(f"{model} answered with content parts, not text")
    # A reasoning model that spends max_tokens on reasoning answers null content: an empty turn, not a crash.
    details = reply.usage.prompt_tokens_details
    return ProviderResult(
        text=content or "",
        prompt_tokens=reply.usage.prompt_tokens,
        completion_tokens=reply.usage.completion_tokens,
        tool_calls=[call.model_dump() for call in choice.message.tool_calls or []] or None,
        cached_tokens=(details.cached_tokens or 0) if details else 0,
    )


def live_provider_from_environment(
    settings: ProviderSettings,
    *,
    dotenv_path: Path | None = None,
    secrets_directory: Path | None = None,
) -> OpenRouterProvider | None:
    """The provider for a run outside the stack (STACK=0), or ``None`` when no source holds a key.

    The key comes from the environment, else ``dotenv_path``, else ``secrets_directory``; a source left
    ``None`` is not read (see :mod:`loc_arena.stack.stack_secrets`).
    """
    key = StackSecrets(_env_file=dotenv_path, _secrets_dir=secrets_directory).openrouter_api_key
    return None if key is None else OpenRouterProvider(settings, key)
