"""Field redaction for structured logs.

Meridian never logs secrets. A :class:`Redactor` masks values whose keys look sensitive (token, password,
credential, api_key, authorization, secret, ...) anywhere in a nested structure, and can mask by value pattern
(anything shaped like a bearer token). Redaction is applied to every structured log record before it is
emitted.
"""

from __future__ import annotations

import re
from typing import Any

_DEFAULT_SENSITIVE = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "credentials",
    "private_key",
    "session",
)
_MASK = "***"
# a value shaped like a signed token/credential (base64url body '.' hex sig) is masked even under a safe key
_TOKENISH = re.compile(r"^[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9]{16,}$")


class Redactor:
    """Masks sensitive keys and token-shaped values in a nested mapping/list structure."""

    def __init__(
        self, sensitive_keys: tuple[str, ...] = _DEFAULT_SENSITIVE, *, mask_tokenish: bool = True
    ) -> None:
        """Hold the lowercased sensitive-key substrings and whether to also mask token-shaped values."""
        self._sensitive = tuple(k.lower() for k in sensitive_keys)
        self._mask_tokenish = mask_tokenish

    def _is_sensitive_key(self, key: str) -> bool:
        low = key.lower()
        return any(s in low for s in self._sensitive)

    def redact(self, value: Any, *, _key: str | None = None) -> Any:
        """Return a copy of ``value`` with sensitive fields and token-shaped strings masked."""
        if _key is not None and self._is_sensitive_key(_key):
            return _MASK
        if isinstance(value, dict):
            return {k: self.redact(v, _key=str(k)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        if isinstance(value, str) and self._mask_tokenish and _TOKENISH.match(value):
            return _MASK
        return value
