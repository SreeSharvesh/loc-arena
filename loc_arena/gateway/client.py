"""In-sandbox inference client.

Enforces mandatory routing: agent code and the data-gen worker reach a model ONLY through this client,
which relays to the gateway edge; there is no direct provider access from a sandbox. ``generate`` is the
raw client (no turn id and no batch id unless a valid turn token is supplied by the scaffold for a
sanctioned turn); it is exactly the affordance a rogue scaffold would abuse off-turn. ``batch_generate``
registers a sanctioned, quota-limited batch. The turn token is supplied by an injected provider so an
off-turn process cannot forge one.
"""

from __future__ import annotations

from collections.abc import Callable

from loc_arena.gateway.core import (
    BatchGenerateRequest,
    BatchGenerateResponse,
    GenerateRequest,
    GenerateResponse,
)
from loc_arena.gateway.transport import Transport


class GatewayClient:
    """The client agent code calls. Points at the edge; carries the identity and a turn-token source."""

    def __init__(
        self,
        edge: Transport,
        caller_identity: str,
        *,
        turn_token_provider: Callable[[], str | None] | None = None,
    ) -> None:
        """Wire the client to a transport to the edge, its caller identity, and a turn-token source.

        If no ``turn_token_provider`` is given, the scaffold sets the current turn's token with
        ``set_turn_token`` at turn boundaries; code running outside a turn (a spawned worker) leaves it
        ``None`` so its calls are off-path.
        """
        self._edge = edge
        self._caller_identity = caller_identity
        self._turn_token: str | None = None
        self._turn_token_provider = (
            turn_token_provider if turn_token_provider is not None else self._current_token
        )

    def _current_token(self) -> str | None:
        return self._turn_token

    def set_turn_token(self, token: str | None) -> None:
        """Set or clear the token for the current sanctioned turn (set by the scaffold at turn boundaries)."""
        self._turn_token = token

    def generate(self, prompt: str, *, role: str = "teacher") -> str:
        """Make one raw model call; carries a turn token only if the scaffold supplied one for this turn."""
        req = GenerateRequest(
            prompt=prompt,
            caller_identity=self._caller_identity,
            role=role,
            turn_token=self._turn_token_provider(),
        )
        reply = self._edge.post_json("/generate", req.model_dump())
        return GenerateResponse.model_validate(reply).text

    def batch_generate(self, prompts: list[str], *, role: str = "teacher") -> list[str]:
        """Register a sanctioned, quota-limited batch and return one completion per prompt."""
        req = BatchGenerateRequest(prompts=prompts, caller_identity=self._caller_identity, role=role)
        reply = self._edge.post_json("/batch_generate", req.model_dump())
        return BatchGenerateResponse.model_validate(reply).texts
