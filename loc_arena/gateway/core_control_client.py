"""The runner's control of one episode on the gateway core, over the core's control routes.

``CoreControlClient`` implements ``GatewayControl`` for the episode it opened: every call is one request
to the core (``loc_arena.gateway.core_service``), made with the control key the ``ServiceClient`` holds,
and returns once the core has applied it, so the runner's clock ticks and coverage changes reach the core
in the order the runner makes them. A refusal raises ``httpx.HTTPStatusError`` (401 without the key, 404
for an episode the core does not hold, 502 when the provider failed a monitor's call).
"""

from __future__ import annotations

from typing import Self

from pydantic import SecretStr

from loc_arena.stack.constants import (
    CLOCK_ROUTE,
    CLOSE_ROUTE,
    COVERAGE_ROUTE,
    DURABLE_CREDENTIALS_ROUTE,
    EPISODES_ROUTE,
    HEALTH_ROUTE,
    MONITOR_CALLS_ROUTE,
    TURN_TOKENS_ROUTE,
)
from loc_arena.stack.contracts import (
    ClockUpdate,
    CoreHealth,
    CoverageComponent,
    CoverageUpdate,
    CredentialRotation,
    DurableCredential,
    DurableCredentialIssued,
    DurableCredentialRequest,
    EpisodeClosed,
    EpisodeHandle,
    EpisodeOpen,
    EpisodeOpened,
    IssuedToken,
    MonitorCall,
    MonitorCallResult,
    TurnTokenRequest,
)
from loc_arena.stack.service_client import ServiceClient


def read_core_health(client: ServiceClient) -> CoreHealth:
    """The core's health: up, and whether it holds a provider key."""
    return CoreHealth.model_validate_json(client.send("GET", HEALTH_ROUTE).content)


class CoreControlClient:
    """``GatewayControl`` of one episode the core holds, over its control routes."""

    def __init__(self, client: ServiceClient, handle: EpisodeHandle, episode_id: str) -> None:
        """Control episode ``handle`` (id ``episode_id``) through ``client``, a control-keyed core client."""
        self.handle = handle
        self.episode_id = episode_id
        self._client = client

    @classmethod
    def open_episode(cls, client: ServiceClient, opening: EpisodeOpen) -> Self:
        """Open an episode on the core (it becomes the one off-path calls are recorded under); control it."""
        opened = client.post_model(EPISODES_ROUTE, opening, EpisodeOpened)
        return cls(client, opened.handle, opened.episode_id)

    def set_clock(self, now: float) -> None:
        """Set the simulated clock the core stamps records and verifies tokens with."""
        self._client.send("PUT", self._route(CLOCK_ROUTE), ClockUpdate(now=now))

    def set_coverage(self, component: CoverageComponent, covered: frozenset[str] | None) -> None:
        """Set the deployed monitor or logging coverage (``None`` covers everyone)."""
        self._client.send(
            "PUT",
            self._route(COVERAGE_ROUTE),
            CoverageUpdate(component=component, covered=covered),
        )

    def mint_turn_token(self, agent_uid: str, turn: int) -> str:
        """A token the core minted, marking ``agent_uid``'s calls in ``turn`` as sanctioned."""
        request = TurnTokenRequest(agent_uid=agent_uid, turn=turn)
        return self._client.post_model(self._route(TURN_TOKENS_ROUTE), request, IssuedToken).token

    def issue_durable_credential(self, account: str, *, sanctioned: bool) -> DurableCredential:
        """Have the core issue a durable credential for ``account``, recording whether it was sanctioned."""
        issued = self._request_durable_credential(account, sanctioned=sanctioned, rotate=False)
        return DurableCredential(token=SecretStr(issued.token), instance=issued.instance)

    def rotate_durable_credential(self, account: str, *, sanctioned: bool) -> CredentialRotation:
        """Have the core revoke ``account``'s live instance and issue a new one."""
        issued = self._request_durable_credential(account, sanctioned=sanctioned, rotate=True)
        return CredentialRotation(
            revoked_instance=issued.revoked_instance,
            credential=DurableCredential(token=SecretStr(issued.token), instance=issued.instance),
        )

    def call_model_as_monitor(self, monitor: str, prompt: str, temperature: float) -> str:
        """A monitor's own model call, made and recorded by the core under a credential it signs."""
        call = MonitorCall(monitor=monitor, prompt=prompt, temperature=temperature)
        return self._client.post_model(self._route(MONITOR_CALLS_ROUTE), call, MonitorCallResult).text

    def close(self) -> int:
        """Close the episode (its model routes stop serving it) and return the last seq of its sealed log."""
        return self._client.post_model(self._route(CLOSE_ROUTE), None, EpisodeClosed).last_sealed_seq

    def _request_durable_credential(
        self,
        account: str,
        *,
        sanctioned: bool,
        rotate: bool,
    ) -> DurableCredentialIssued:
        request = DurableCredentialRequest(account=account, sanctioned=sanctioned, rotate=rotate)
        return self._client.post_model(
            self._route(DURABLE_CREDENTIALS_ROUTE),
            request,
            DurableCredentialIssued,
        )

    def _route(self, template: str) -> str:
        return template.format(handle=self.handle)
