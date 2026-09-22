"""A typed, retrying client to the identity service, with a small credential cache.

:class:`AuthClient` issues, refreshes, and validates credentials through an injected
:class:`~meridian_common.authclient.models.AuthTransport`, retrying transient transport failures. It caches
the
most recent credential per account and refreshes it before expiry (with a configurable skew), so callers get a
live credential without minting a new one on every call.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from meridian_common.authclient.models import AuthTransport, Credential
from meridian_common.errors import AuthError, TransportError, ValidationError
from meridian_common.retry.backoff import BackoffPolicy, ExponentialBackoff
from meridian_common.retry.circuit import retry_call


def _parse_credential(data: dict[str, object]) -> Credential:
    try:
        return Credential(
            token=str(data["token"]),
            account=str(data["account"]),
            instance=str(data["instance"]),
            durable=bool(data["durable"]),
            expires_at=float(data["expires_at"]),  # type: ignore[arg-type]
            sanctioned=bool(data.get("sanctioned", True)),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ValidationError(f"bad credential payload: {data!r}", path="credential") from exc


class AuthClient:
    """A retrying, caching facade over an :class:`AuthTransport`."""

    def __init__(
        self,
        transport: AuthTransport,
        *,
        max_attempts: int = 3,
        backoff: BackoffPolicy | None = None,
        refresh_skew: float = 5.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Wire the client to its transport, retry policy, refresh skew, and clocks."""
        self._transport = transport
        self._max_attempts = max_attempts
        self._backoff = backoff if backoff is not None else ExponentialBackoff(base=0.05, cap=2.0)
        self._refresh_skew = refresh_skew
        self._clock = clock
        self._sleep = sleep
        self._cache: dict[str, Credential] = {}

    def _call(self, fn: Callable[[], object]) -> dict[str, object]:
        result = retry_call(
            fn,
            max_attempts=self._max_attempts,
            backoff=self._backoff,
            sleep=self._sleep,
            retry_on=(TransportError,),
        )
        if not isinstance(result, dict):
            raise ValidationError("auth transport returned a non-mapping", path="response")
        return result

    def issue(self, account: str, *, durable: bool = False, sanctioned: bool = True) -> Credential:
        """Issue a credential for ``account`` and cache it."""
        cred = _parse_credential(
            self._call(lambda: self._transport.issue(account, durable=durable, sanctioned=sanctioned))
        )
        self._cache[account] = cred
        return cred

    def refresh(self, credential: Credential) -> Credential:
        """Refresh ``credential`` to a new instance and update the cache."""
        cred = _parse_credential(self._call(lambda: self._transport.refresh(credential.token)))
        self._cache[cred.account] = cred
        return cred

    def validate(self, token: str) -> bool:
        """Whether ``token`` is currently valid according to the identity service."""
        result = self._call(lambda: self._transport.validate(token))
        return bool(result.get("valid", False))

    def get_valid(self, account: str, *, durable: bool = False, sanctioned: bool = True) -> Credential:
        """Return a live credential for ``account``, issuing or refreshing before expiry as needed."""
        cached = self._cache.get(account)
        now = self._clock()
        if cached is None:
            return self.issue(account, durable=durable, sanctioned=sanctioned)
        if cached.expires_at - now <= self._refresh_skew:
            try:
                return self.refresh(cached)
            except AuthError:
                return self.issue(account, durable=durable, sanctioned=sanctioned)
        return cached
