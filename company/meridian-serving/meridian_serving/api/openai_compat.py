"""Translation to and from an OpenAI-style completion schema.

External clients speak an OpenAI-shaped JSON API; the serving stack speaks :class:`~meridian_serving.types.
Request`/:class:`~meridian_serving.types.Response`. This module validates an incoming request payload (via
meridian-common's schema layer), maps it to an internal ``Request`` with its sampling parameters, and
renders an
internal ``Response`` back into the wire shape, so the endpoint stays decoupled from the public contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from meridian_common.config import Field, Schema
from meridian_serving.sampler.sampling import Sampler
from meridian_serving.types import Priority, Request, Response

_REQUEST_SCHEMA = Schema(
    {
        "id": Field(str),
        "prompt": Field(list, item=Field(int)),
        "max_tokens": Field(int, required=False, default=16, validator=lambda v: v >= 0),
        "temperature": Field(float, required=False, default=1.0, validator=lambda v: v > 0),
        "top_p": Field(float, required=False, default=1.0, validator=lambda v: 0.0 < v <= 1.0),
        "top_k": Field(int, required=False, default=0),
        "priority": Field(str, required=False, default="normal"),
    },
    allow_extra=True,
)

_PRIORITY = {
    "low": Priority.LOW,
    "normal": Priority.NORMAL,
    "high": Priority.HIGH,
    "critical": Priority.CRITICAL,
}


@dataclass(frozen=True)
class ParsedRequest:
    """A validated request plus the sampler its parameters imply."""

    request: Request
    sampler: Sampler


def parse_request(payload: dict[str, Any]) -> ParsedRequest:
    """Validate an OpenAI-style payload and map it to an internal request and its sampler."""
    data = _REQUEST_SCHEMA.validate(payload)
    request = Request(
        request_id=str(data["id"]),
        prompt=tuple(int(t) for t in data["prompt"]),
        max_tokens=int(data["max_tokens"]),
        priority=_PRIORITY.get(str(data["priority"]).lower(), Priority.NORMAL),
    )
    sampler = Sampler(
        temperature=float(data["temperature"]),
        top_p=float(data["top_p"]),
        top_k=int(data["top_k"]),
    )
    return ParsedRequest(request=request, sampler=sampler)


def render_response(response: Response, *, model: str = "meridian-serving") -> dict[str, Any]:
    """Render an internal response into the OpenAI-style completion shape."""
    return {
        "id": response.request_id,
        "object": "text_completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "tokens": list(response.tokens),
                "finish_reason": response.finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": response.prompt_len,
            "completion_tokens": response.generated_len,
            "total_tokens": response.prompt_len + response.generated_len,
        },
    }
