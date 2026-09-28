"""The OpenRouter provider: the one egress to a model, through the official SDK, bounded in time."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from pathlib import Path
from typing import Final

import httpx
import stamina
from openrouter import OpenRouter, components, errors
from openrouter.utils import BackoffStrategy, RetryConfig
from pydantic import BaseModel, SecretStr, ValidationError

from loc_arena.gateway.core import Message, ProviderError, ProviderResult, ToolSpec
from loc_arena.stack.settings import ProviderSettings
from loc_arena.stack.stack_secrets import StackSecrets

MILLISECONDS_PER_SECOND: Final = 1000
TRUNCATION_MARKER: Final = " [cut]"  # ends an error message cut at error_message_max_characters


class ProviderTimeoutError(ProviderError):
    """A request outlived its deadline, or the call with its retries outlived its budget."""


class ProviderReplyError(ProviderError):
    """The provider answered 200 without a usable completion: an error body, no choice, or no token usage."""


class _ErrorDetail(BaseModel):
    code: int
    message: str


class _ErrorBody(BaseModel):
    error: _ErrorDetail


# The timeout and retry layers: docs/isolation/design.md#openrouter-provider.
class _DeadlineTransport(httpx.AsyncHTTPTransport):
    def __init__(self, deadline_seconds: float) -> None:
        super().__init__()
        self._deadline_seconds = deadline_seconds

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
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
            strategy="backoff",
            backoff=BackoffStrategy(
                initial_interval=settings.backoff_initial_interval_milliseconds,
                max_interval=settings.backoff_max_interval_milliseconds,
                exponent=settings.backoff_exponent,
                max_elapsed_time=settings.backoff_max_elapsed_time_milliseconds,
            ),
            retry_connection_errors=settings.retry_connection_errors,
        )
        # A retrying caller: stamina's retry log then holds no call argument, so no message.
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
        """Send ``messages`` and any ``tools`` to ``model``; return the completion and its usage."""
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
            max_characters = self._settings.error_message_max_characters
            raise build_unusable_reply_error(model, error, max_characters) from error
        except ValidationError as error:  # the SDK refused to build the request; its text quotes the messages
            reason = f"the SDK cannot send these messages or tools ({error.error_count()} errors)"
            raise ProviderError(f"{model}: {reason}") from error
        except errors.OpenRouterError as error:
            message = cut_message(error.message, self._settings.error_message_max_characters)
            raise ProviderError(
                f"{model}: {type(error).__name__} (HTTP {error.status_code}): {message}",
                status_code=error.status_code,
            ) from error
        except (errors.NoResponseError, httpx.HTTPError) as error:
            message = cut_message(str(error), self._settings.error_message_max_characters)
            raise ProviderError(f"{model}: {type(error).__name__}: {message}") from error
        return _read_completion(model, reply)

    async def _complete(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> components.ChatResult:
        async with (
            asyncio.timeout(self._settings.backoff_max_elapsed_time_milliseconds / MILLISECONDS_PER_SECOND),
            # trust_env=False: an environment proxy would get httpx's own transport, without the deadline.
            httpx.AsyncClient(
                transport=_DeadlineTransport(self._settings.request_deadline_seconds),
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
                tools=tools or None,
                temperature=temperature,
                max_completion_tokens=max_tokens,
                stream=False,
            )

    def _wait_before_retrying(self, error: Exception) -> bool | float:
        if not isinstance(error, errors.OpenRouterError) or error.status_code != HTTPStatus.TOO_MANY_REQUESTS:
            return False
        try:
            retry_after_seconds = float(error.headers.get("retry-after", ""))
        except ValueError:
            return True
        if retry_after_seconds >= 0:  # false for NaN too
            return min(retry_after_seconds, self._settings.rate_limit_max_wait_seconds)
        return True


def build_unusable_reply_error(
    model: str,
    error: errors.ResponseValidationError,
    max_characters: int,
) -> ProviderReplyError:
    """Say why a reply is no chat completion, quoting OpenRouter's error and its code when the body is one."""
    try:
        detail = _ErrorBody.model_validate_json(error.body).error
    except ValidationError:
        reason = f"{model} answered {error.status_code} with a body that is not a chat completion"
        return ProviderReplyError(reason)
    message = cut_message(detail.message, max_characters)
    reason = f"{model} answered {error.status_code} with error {detail.code}: {message}"
    return ProviderReplyError(reason, status_code=detail.code)


def cut_message(message: str, max_characters: int) -> str:
    """``message``, or its first ``max_characters`` characters and ``TRUNCATION_MARKER`` when it is longer."""
    return message if len(message) <= max_characters else message[:max_characters] + TRUNCATION_MARKER


def _read_completion(model: str, reply: components.ChatResult) -> ProviderResult:
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
    # Null content: a reasoning model spent max_tokens on reasoning; the turn is empty.
    details = reply.usage.prompt_tokens_details
    cost = reply.usage.cost
    return ProviderResult(
        text=content or "",
        prompt_tokens=reply.usage.prompt_tokens,
        completion_tokens=reply.usage.completion_tokens,
        tool_calls=[call.model_dump() for call in choice.message.tool_calls or []] or None,
        cached_tokens=(details.cached_tokens or 0) if details else 0,
        cost=cost if isinstance(cost, float) else None,  # absent (the SDK's UNSET) or null: unknown
    )


def live_provider_from_environment(
    settings: ProviderSettings,
    *,
    dotenv_path: Path | None = None,
) -> OpenRouterProvider | None:
    """The provider for a run outside the stack (STACK=0), or ``None`` when no source holds a key."""
    key = StackSecrets(_env_file=dotenv_path, _secrets_dir=None).openrouter_api_key
    return None if key is None else OpenRouterProvider(settings, key)
