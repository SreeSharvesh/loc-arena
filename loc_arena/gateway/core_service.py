"""The gateway core as a FastAPI service: one GatewayCore per episode handle, and the control routes.

The model routes (``/generate``, ``/batch_generate``) serve the ACTIVE episode, the one opened last and
not yet closed, and answer 409 when there is none; the edge relays every agent call there. The control
routes need the control key: the runner opens an episode, sets its clock and coverage, has the core mint
turn tokens and durable credentials, records its sealed events, makes monitor calls and closes it. Each
episode's sealed records reach the recorder through its ``EpisodeLedger``.

No ``from __future__ import annotations``: the routes' dependency annotations name types local to
:func:`create_core_app`, which FastAPI must resolve at runtime (see :mod:`loc_arena.stack.service_client`).
"""

import threading
from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import SecretStr
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from loc_arena.gateway.control import EpisodeLedger, LocalGatewayControl
from loc_arena.gateway.core import DeterministicProvider, EpisodeSpec, Provider
from loc_arena.gateway.openrouter_provider import OpenRouterProvider, ProviderError
from loc_arena.logging_.events import Event, EventDraft
from loc_arena.stack.constants import (
    BATCH_GENERATE_ROUTE,
    CLOCK_ROUTE,
    CLOSE_ROUTE,
    COVERAGE_ROUTE,
    DURABLE_CREDENTIALS_ROUTE,
    EPISODES_ROUTE,
    GENERATE_ROUTE,
    HEALTH_ROUTE,
    MONITOR_CALLS_ROUTE,
    RECORDER_HOSTNAME,
    SEALED_EVENTS_ROUTE,
    TURN_TOKENS_ROUTE,
    build_service_url,
)
from loc_arena.stack.contracts import (
    BatchGenerateRequest,
    BatchGenerateResponse,
    ClockUpdate,
    CoreGenerateResponse,
    CoreHealth,
    CoverageUpdate,
    DurableCredentialIssued,
    DurableCredentialRequest,
    EpisodeClosed,
    EpisodeHandle,
    EpisodeOpen,
    EpisodeOpened,
    GenerateRequest,
    IssuedToken,
    MonitorCall,
    MonitorCallResult,
    ProviderKind,
    TurnTokenRequest,
    build_episode_id,
    generate_episode_handle,
)
from loc_arena.stack.service_client import ServiceClient, require_control_key
from loc_arena.stack.settings import LocArenaSettings, load_settings_from_environment
from loc_arena.stack.stack_secrets import load_container_secrets


class CoreEpisodes:
    """The core's episodes by handle, and the active one its model routes serve."""

    def __init__(
        self,
        settings: LocArenaSettings,
        recorder: ServiceClient,
        provider: Provider | None,
    ) -> None:
        """Open episodes under ``settings``, recording through ``recorder`` (``provider`` is None keyless)."""
        self.settings = settings
        self._recorder = recorder
        self._provider = provider
        self._episodes: dict[str, LocalGatewayControl] = {}
        self._active: LocalGatewayControl | None = None
        self._lock = threading.Lock()

    @property
    def provider_configured(self) -> bool:
        """Whether the core holds a provider key, so an episode may use the ``openrouter`` provider."""
        return self._provider is not None

    @property
    def active(self) -> LocalGatewayControl | None:
        """The episode the model routes serve, if one is open."""
        return self._active

    def find(self, handle: str) -> LocalGatewayControl | None:
        """The episode ``handle`` names, if the core opened it."""
        return self._episodes.get(handle)

    def open(self, opening: EpisodeOpen) -> EpisodeOpened:
        """Open a fresh episode (new handle, keys, clock, quota) and make it the active one."""
        provider = self._choose_provider(opening.provider)
        handle = generate_episode_handle()
        episode_id = build_episode_id(opening.run_config_name, opening.mode)
        ledger = EpisodeLedger(self._recorder, handle, episode_id, settings=self.settings.gateway)
        spec = EpisodeSpec(
            models=opening.models,
            covert=opening.covert,
            teacher_token_quota=self.settings.inference.batch_generate.teacher_token_quota,
        )
        episode = LocalGatewayControl.open(
            spec,
            episode_id=episode_id,
            handle=handle,
            sealed=ledger,
            provider=provider,
            settings=self.settings,
            observer=ledger,
        )
        with self._lock:
            self._episodes[handle] = episode
            self._active = episode
        return EpisodeOpened(handle=handle, episode_id=episode_id)

    def deactivate(self, episode: LocalGatewayControl) -> None:
        """Stop serving model calls under ``episode``; its control routes stay open."""
        with self._lock:
            if self._active is episode:
                self._active = None

    def _choose_provider(self, kind: ProviderKind) -> Provider:
        if kind == "deterministic":
            return DeterministicProvider()  # one per episode, as in process: its outputs count from gen#1
        if self._provider is None:
            raise HTTPException(
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
                detail="the core holds no provider key; open the episode with the deterministic provider",
            )
        return self._provider


def create_core_app(episodes: CoreEpisodes, *, control_key: SecretStr) -> FastAPI:
    """Wire the core's model routes and control routes (control key) onto a FastAPI app."""
    gateway = episodes.settings.gateway
    app = FastAPI(
        title="loc-arena gateway core",
        openapi_url=None,
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=gateway.max_request_bytes)],
    )
    control = APIRouter(dependencies=[Depends(require_control_key(control_key, gateway.control_key_header))])

    def find_episode(handle: EpisodeHandle) -> LocalGatewayControl:
        episode = episodes.find(handle)
        if episode is None:
            raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=f"no episode {handle}")
        return episode

    def find_active_episode() -> LocalGatewayControl:
        episode = episodes.active
        if episode is None:
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail="no episode is active")
        return episode

    Episode = Annotated[LocalGatewayControl, Depends(find_episode)]  # noqa: N806 - a type alias
    ActiveEpisode = Annotated[LocalGatewayControl, Depends(find_active_episode)]  # noqa: N806 - a type alias

    @app.exception_handler(ProviderError)
    def report_provider_failure(request: Request, error: ProviderError) -> JSONResponse:
        return JSONResponse(
            {"detail": "the model provider failed this call"},
            status_code=HTTPStatus.BAD_GATEWAY,
        )

    @app.get(HEALTH_ROUTE)
    def report_health() -> CoreHealth:
        return CoreHealth(ok=True, provider_configured=episodes.provider_configured)

    # Plain ``def`` routes: FastAPI runs them in its threadpool, where the provider may block.
    @app.post(GENERATE_ROUTE)
    def generate(request: GenerateRequest, episode: ActiveEpisode) -> CoreGenerateResponse:
        return episode.core.generate(request)

    @app.post(BATCH_GENERATE_ROUTE)
    def batch_generate(request: BatchGenerateRequest, episode: ActiveEpisode) -> BatchGenerateResponse:
        return episode.core.batch_generate(request)

    @control.post(EPISODES_ROUTE, status_code=HTTPStatus.CREATED)
    def open_episode(opening: EpisodeOpen) -> EpisodeOpened:
        return episodes.open(opening)

    @control.put(CLOCK_ROUTE, status_code=HTTPStatus.NO_CONTENT)
    def set_clock(update: ClockUpdate, episode: Episode) -> None:
        episode.set_clock(update.now)

    @control.put(COVERAGE_ROUTE, status_code=HTTPStatus.NO_CONTENT)
    def set_coverage(update: CoverageUpdate, episode: Episode) -> None:
        episode.set_coverage(update.component, update.covered)

    @control.post(TURN_TOKENS_ROUTE)
    def mint_turn_token(request: TurnTokenRequest, episode: Episode) -> IssuedToken:
        return IssuedToken(token=episode.mint_turn_token(request.agent_uid, request.turn))

    @control.post(DURABLE_CREDENTIALS_ROUTE)
    def issue_durable_credential(
        request: DurableCredentialRequest,
        episode: Episode,
    ) -> DurableCredentialIssued:
        if request.rotate:
            rotation = episode.rotate_durable_credential(request.account, sanctioned=request.sanctioned)
            revoked_instance, credential = rotation.revoked_instance, rotation.credential
        else:
            revoked_instance = ""
            credential = episode.issue_durable_credential(request.account, sanctioned=request.sanctioned)
        return DurableCredentialIssued(
            token=credential.token.get_secret_value(),
            instance=credential.instance,
            revoked_instance=revoked_instance,
        )

    @control.post(SEALED_EVENTS_ROUTE)
    def record_sealed_event(draft: EventDraft, episode: Episode) -> Event:
        return episode.sealed.record(draft)

    @control.post(MONITOR_CALLS_ROUTE)
    def call_model_as_monitor(call: MonitorCall, episode: Episode) -> MonitorCallResult:
        return MonitorCallResult(
            text=episode.call_model_as_monitor(call.monitor, call.prompt, call.temperature),
        )

    @control.post(CLOSE_ROUTE)
    def close_episode(episode: Episode) -> EpisodeClosed:
        episodes.deactivate(episode)
        return EpisodeClosed(last_sealed_seq=episode.close())

    app.include_router(control)
    return app


def build_core_app() -> FastAPI:
    """The core service for ``uvicorn --factory``: settings from the environment, keys from /run/secrets."""
    settings = load_settings_from_environment()
    stack_secrets = load_container_secrets()
    if stack_secrets.control_key is None:
        raise RuntimeError("no control_key secret: the core will not serve its control routes unguarded")
    api_key = stack_secrets.openrouter_api_key
    provider = None if api_key is None else OpenRouterProvider(settings.provider, api_key)
    recorder = ServiceClient.connect(
        build_service_url(RECORDER_HOSTNAME, settings.gateway.recorder_port),
        timeout_seconds=settings.gateway.recorder_timeout_seconds,
    )
    return create_core_app(CoreEpisodes(settings, recorder, provider), control_key=stack_secrets.control_key)
