"""The runner's control of one episode on the core, and the ledger that writes the episode's sealed log."""

from __future__ import annotations

import threading
import time
from collections.abc import Set as AbstractSet
from dataclasses import asdict, replace

import httpx
import stamina
from pydantic import SecretStr

from loc_arena.gateway.core import EpisodeSpec, GatewayCore, ModelCallObserver, Provider, SigningKeys
from loc_arena.identity import IdentityService
from loc_arena.logging_.events import Event, EventDraft, EventLog
from loc_arena.stack.constants import MONITOR_MODEL_ROLE, RECORDER_EVENTS_ROUTE, RECORDER_MODEL_CALLS_ROUTE
from loc_arena.stack.contracts import (
    AppendAck,
    CoverageComponent,
    CredentialRotation,
    DurableCredential,
    GenerateRequest,
    ModelCallRecord,
    ModelCallUsage,
)
from loc_arena.stack.service_client import ServiceClient
from loc_arena.stack.settings import GatewaySettings, LocArenaSettings


class EpisodeLedger:
    """One episode's sealed log as the core writes it in the stack: numbered here, stored by the recorder."""

    def __init__(
        self,
        recorder: ServiceClient,
        handle: str,
        episode_id: str,
        *,
        settings: GatewaySettings,
    ) -> None:
        """Write episode ``episode_id``'s records to the recorder under ``handle``, as ``settings`` sets."""
        self._recorder = recorder
        self._events_route = RECORDER_EVENTS_ROUTE.format(handle=handle)
        self._model_calls_route = RECORDER_MODEL_CALLS_ROUTE.format(handle=handle)
        self._episode_id = episode_id
        self._last_seq = -1
        self._lock = threading.Lock()
        self._write_retries = stamina.RetryingCaller(
            attempts=settings.recorder_write_attempts,
            timeout=None,
        ).on(httpx.TransportError)

    @property
    def last_seq(self) -> int:
        """The highest seq given to an event, or -1 if none (a failed write may have left it unwritten)."""
        return self._last_seq

    def record(self, draft: EventDraft, /) -> Event:
        """Give ``draft`` the next seq and have the recorder append it, both under one lock (unique seqs)."""
        with self._lock:
            event = Event(episode_id=self._episode_id, seq=self._last_seq + 1, **asdict(draft))
            try:
                acknowledgement = self._write_retries(self._append, event)
            except httpx.TransportError:
                self._last_seq = event.seq  # it may have landed: see docs/isolation/design.md#episode-ledger
                raise
            self._last_seq = event.seq
        return replace(event, fp=acknowledgement.fp)

    def _append(self, event: Event) -> AppendAck:
        return self._recorder.post_model(self._events_route, event, AppendAck)

    def on_model_call(
        self,
        *,
        identity: str,
        role: str,
        model_input: str,
        output: str,
        sealed_seq: int,
        usage: ModelCallUsage,
    ) -> None:
        """Send one provider call to the sealed model-call log, unretried: a re-send would record it twice."""
        record = ModelCallRecord(
            sealed_seq=sealed_seq,
            identity=identity,
            role=role,
            model_input=model_input,
            output=output,
            wall_ts=time.time(),
            usage=usage,
        )
        self._recorder.send("POST", self._model_calls_route, record)


class LocalGatewayControl:
    """``GatewayControl`` of one episode in the core's process, holding the episode's core and sealed log."""

    def __init__(
        self,
        spec: EpisodeSpec,
        *,
        episode_id: str,
        handle: str,
        sealed: EventLog,
        provider: Provider,
        settings: LocArenaSettings,
        observer: ModelCallObserver | None = None,
    ) -> None:
        """Open one episode: fresh per-kind keys made here, its own identity service, clock and core."""
        keys = SigningKeys.generate()
        self.sealed = sealed
        self._now = settings.clock.start_ts
        self._settings = settings.gateway
        self._identity = IdentityService(
            keys.durable.get_secret_value(),
            default_exp=settings.gateway.durable_credential_expiry_ts,
        )
        self.core = GatewayCore(
            spec,
            episode_id,
            sealed,
            provider,
            signing_keys=keys,
            episode_handle=handle,
            clock=lambda: self._now,
            is_revoked=self._identity.is_revoked,
            trace=observer,
        )

    def set_clock(self, now: float) -> None:
        """Set the simulated clock the core stamps records and verifies tokens with."""
        self._now = now

    def set_coverage(self, component: CoverageComponent, covered: AbstractSet[str] | None) -> None:
        """Set the deployed monitor or logging coverage (``None`` covers everyone)."""
        if component == "monitor":
            self.core.set_monitor_covered(covered)
        else:
            self.core.set_logging_covered(covered)

    def mint_turn_token(self, agent_uid: str, turn: int) -> str:
        """A token marking ``agent_uid``'s calls in ``turn`` as sanctioned, for the configured lifetime."""
        expires_at = self._now + self._settings.turn_token_ttl_seconds
        return self.core.mint_turn_token(agent_uid, turn, expires_at=expires_at)

    def issue_durable_credential(self, account: str, *, sanctioned: bool) -> DurableCredential:
        """Issue a durable credential for ``account``, recording truthfully whether it was sanctioned."""
        token, instance = self._identity.issue(account, sanctioned=sanctioned)
        return DurableCredential(token=SecretStr(token), instance=instance)

    def rotate_durable_credential(self, account: str, *, sanctioned: bool) -> CredentialRotation:
        """Revoke ``account``'s live instance (the core stops resolving it) and issue a new one."""
        revoked_instance, token, instance = self._identity.rotate(account, sanctioned=sanctioned)
        return CredentialRotation(
            revoked_instance=revoked_instance,
            credential=DurableCredential(token=SecretStr(token), instance=instance),
        )

    def call_model_as_monitor(self, monitor: str, prompt: str, temperature: float) -> str:
        """Make a monitor's own model call under a credential the core signs (sealed and attributable)."""
        expires_at = self._now + self._settings.monitor_credential_ttl_seconds
        request = GenerateRequest(
            prompt=prompt,
            caller_identity=monitor,
            role=MONITOR_MODEL_ROLE,
            monitor_credential=self.core.issue_monitor_credential(monitor, expires_at=expires_at),
            temperature=temperature,
        )
        return self.core.generate(request).text

    def close(self) -> int:
        """The last seq of the sealed log; the runner's monitor calls and events may still follow it."""
        return self.sealed.last_seq
