"""The stack's one HTTP client helper and its one control-key check.

``ServiceClient`` sends a typed body to a service and parses its typed reply over httpx;
``require_control_key`` is the FastAPI dependency every control route and mirror write uses (FastAPI
"Security" tutorial: an ``APIKeyHeader`` scheme, the key compared with ``hmac.compare_digest``).

No ``from __future__ import annotations`` here: FastAPI reads a dependency's annotations at runtime, and
``check_control_key``'s annotation names a scheme local to ``require_control_key``, which a string
annotation could not resolve.
"""

import hmac
from collections.abc import Callable
from functools import cache
from http import HTTPStatus
from typing import Annotated, Any, Literal, Self

import httpx
from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, SecretStr, TypeAdapter

from loc_arena.logging_.events import Event, EventDraft
from loc_arena.stack.constants import DEFAULT_CONTROL_KEY_HEADER

HttpMethod = Literal["GET", "POST", "PUT"]
# A body or a reply: a contract model, or a log dataclass (the event routes carry EventDraft and Event).
Body = BaseModel | EventDraft | Event


@cache
def _adapter(body_type: type[Any]) -> TypeAdapter[Any]:
    """One TypeAdapter per type, built once (pydantic docs: reuse TypeAdapters, building one is costly)."""
    return TypeAdapter(body_type)


class ServiceClient:
    """Talks to one service: JSON bodies in and out, and the control key on every request when it holds one.

    A non-2xx reply raises ``httpx.HTTPStatusError``. Tests pass a FastAPI ``TestClient`` as ``http_client``.
    """

    def __init__(
        self,
        http_client: httpx.Client,
        *,
        control_key: SecretStr | None = None,
        control_key_header: str = DEFAULT_CONTROL_KEY_HEADER,
    ) -> None:
        """Wrap ``http_client`` (its base URL and timeout already set); sign requests with ``control_key``."""
        self._http_client = http_client
        self._control_key = control_key
        self._control_key_header = control_key_header

    @classmethod
    def connect(
        cls,
        base_url: str,
        *,
        timeout_seconds: float,
        control_key: SecretStr | None = None,
        control_key_header: str = DEFAULT_CONTROL_KEY_HEADER,
    ) -> Self:
        """A client of the service at ``base_url``, each request bounded by ``timeout_seconds``."""
        http_client = httpx.Client(base_url=base_url, timeout=timeout_seconds)
        return cls(http_client, control_key=control_key, control_key_header=control_key_header)

    def send(self, method: HttpMethod, path: str, body: Body | None = None) -> httpx.Response:
        """Send ``body`` as JSON (none if ``None``) and return the successful response."""
        headers = {} if body is None else {"Content-Type": "application/json"}
        if self._control_key is not None:
            headers[self._control_key_header] = self._control_key.get_secret_value()
        content = None if body is None else _adapter(type(body)).dump_json(body)
        response = self._http_client.request(method, path, content=content, headers=headers)
        response.raise_for_status()
        return response

    def post_model[ReplyT: Body](self, path: str, body: Body | None, reply_type: type[ReplyT]) -> ReplyT:
        """POST ``body`` and parse the reply as ``reply_type``."""
        reply: ReplyT = _adapter(reply_type).validate_json(self.send("POST", path, body).content)
        return reply

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._http_client.close()


def require_control_key(
    expected_key: SecretStr,
    header_name: str = DEFAULT_CONTROL_KEY_HEADER,
) -> Callable[[str | None], None]:
    """The dependency guarding a route: 401 unless the request's ``header_name`` carries ``expected_key``.

    Use as ``APIRouter(dependencies=[Depends(require_control_key(key, header))])``. A missing header and a
    wrong key get the same answer, 401 with the ``WWW-Authenticate`` header RFC 9110 requires.
    """
    expected = expected_key.get_secret_value().encode()
    if not expected:
        raise ValueError("the control key is empty; refusing to guard routes with it")
    scheme = APIKeyHeader(name=header_name, auto_error=False)

    def check_control_key(presented: Annotated[str | None, Security(scheme)]) -> None:
        if presented is None or not hmac.compare_digest(presented.encode(), expected):
            raise HTTPException(
                status_code=HTTPStatus.UNAUTHORIZED,
                detail="missing or wrong control key",
                headers={"WWW-Authenticate": "APIKey"},
            )

    return check_control_key
