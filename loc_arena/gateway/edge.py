"""Tamperable gateway edge (the mirror side), and its FastAPI service.

Enforces mandatory inference routing (agents reach a model ONLY by relaying through here to the sealed
core) and the sealed-vs-tamperable isolation (it is the tamperable side: it holds no provider key, has
no egress, and writes only the MIRROR log). It relays each call to the core verbatim (passing the caller
identity and any credential through) and writes a mirror record from the ORIGINAL input (a prompt or a
chat history) and the core's returned stamps and ``ts``, so the covert text the core may have injected
never appears in what a monitor reads. The mirror-vs-sealed divergence is a first-class signal. In the
stack it is the only writer of the mirror, ``/mirror/<handle>/events.jsonl``, and takes the runner's
observable events on a route guarded by the control key.

It runs from the sandbox image, which ships no ``configs/``, ``scenarios/`` or ``live.py``: this module
imports only :mod:`loc_arena.stack`, :mod:`loc_arena.logging_.events`, the standard library and
third-party packages. No ``from __future__ import annotations``: FastAPI reads the route annotations at
runtime (see :mod:`loc_arena.stack.service_client`).
"""

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Protocol

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from pydantic import SecretStr
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from loc_arena.logging_.events import AppendOnlyLog, Event, EventDraft, EventLog, fingerprint
from loc_arena.stack.constants import (
    BATCH_GENERATE_ROUTE,
    EPISODE_HANDLE_PATTERN,
    EVENTS_FILE_NAME,
    GATEWAY_CORE_HOSTNAME,
    GENERATE_ROUTE,
    HEALTH_ROUTE,
    MIRROR_EVENTS_ROUTE,
    MIRROR_MOUNT_PATH,
    build_service_url,
)
from loc_arena.stack.contracts import (
    BatchGenerateRequest,
    BatchGenerateResponse,
    CoreGenerateResponse,
    CoreRelay,
    EpisodeHandle,
    MirrorAppend,
    RelayedBatchGenerateResponse,
    RelayedGenerateResponse,
    ServiceHealth,
)
from loc_arena.stack.model_call import GenerateRequest, input_fingerprint, output_fingerprint
from loc_arena.stack.service_client import ServiceClient, require_control_key
from loc_arena.stack.settings import GatewaySettings, load_settings_from_environment
from loc_arena.stack.stack_secrets import load_container_secrets


class MirrorLogs(Protocol):
    """Where the edge writes an episode's mirror: its one log in process, a file per handle in the stack."""

    def log_for(self, handle: str, episode_id: str) -> EventLog:
        """The mirror log of episode ``episode_id`` (handle ``handle``); ``ValueError`` if they disagree."""
        ...


class EpisodeMirror:
    """The mirror log of one episode."""

    def __init__(self, episode_id: str, log: EventLog) -> None:
        """Hold ``episode_id``'s mirror log."""
        self._episode_id = episode_id
        self._log = log

    def log_for(self, handle: str, episode_id: str) -> EventLog:
        """This episode's log; ``ValueError`` if the write belongs to another episode."""
        if episode_id != self._episode_id:
            raise ValueError(f"episode {episode_id!r} is not this mirror's episode {self._episode_id!r}")
        return self._log


class MirrorDirectory:
    """Every episode's mirror under one root, ``<root>/<handle>/events.jsonl``, opened at its first write."""

    def __init__(self, root: Path) -> None:
        """Keep the mirrors under ``root`` (the mirror volume in the stack)."""
        self._root = root
        self._mirrors: dict[str, EpisodeMirror] = {}
        self._lock = threading.Lock()

    def log_for(self, handle: str, episode_id: str) -> EventLog:
        """The mirror of ``handle``, bound to the episode of its first write; ``ValueError`` on another."""
        if re.fullmatch(EPISODE_HANDLE_PATTERN, handle) is None:  # the handle names a directory
            raise ValueError(f"not an episode handle: {handle!r}")
        with self._lock:
            if handle not in self._mirrors:
                log = AppendOnlyLog(self._root / handle / EVENTS_FILE_NAME, episode_id)
                self._mirrors[handle] = EpisodeMirror(episode_id, log)
            mirror = self._mirrors[handle]
        return mirror.log_for(handle, episode_id)


class CoreClient:
    """The core's model routes over control-net: what the edge relays to in the stack (a ``CoreRelay``)."""

    def __init__(self, client: ServiceClient) -> None:
        """Relay through ``client`` (the core's base URL and the relay timeout already set)."""
        self._client = client

    def generate(self, request: GenerateRequest, /) -> CoreGenerateResponse:
        """Relay one model call; a refusal raises ``httpx.HTTPStatusError`` with the core's status."""
        return self._client.post_model(GENERATE_ROUTE, request, CoreGenerateResponse)

    def batch_generate(self, request: BatchGenerateRequest, /) -> BatchGenerateResponse:
        """Relay one batch; a refusal raises ``httpx.HTTPStatusError`` with the core's status."""
        return self._client.post_model(BATCH_GENERATE_ROUTE, request, BatchGenerateResponse)


@dataclass(frozen=True)
class MirroredCall:
    """What a monitor may see of one model call: the fingerprints of its original input and of its reply.

    Never the injected covert text: ``prompt_fp`` covers the input the caller sent, before the core's
    injection. ``credential_kind`` is the core's resolved kind, as its reply carries it.
    """

    caller_identity: str
    turn_id: str | None
    batch_id: str | None
    credential_kind: str
    prompt_fp: str
    output_fp: str
    model_role: str

    def draft(self, ts: float) -> EventDraft:
        """The mirror's inference-call event at the core's ``ts`` (the inference monitor reads it)."""
        return EventDraft(
            ts=ts,
            actor_uid=self.caller_identity,
            actor_role="untrusted",
            kind="inference_call",
            payload={
                "caller_identity": self.caller_identity,
                "turn_id": self.turn_id,
                "batch_id": self.batch_id,
                "credential_kind": self.credential_kind,
                "prompt_fp": self.prompt_fp,
                "output_fp": self.output_fp,
                "model_role": self.model_role,
            },
        )


class MirroringRelay:
    """The edge logic (framework-independent): relay each call to the core, mirror it into its episode's log.

    The core decides the episode (the active one), so the mirror is chosen from its reply's handle and id.
    """

    def __init__(self, core: CoreRelay, mirrors: MirrorLogs) -> None:
        """Wire the edge to the core it relays to and the mirrors it writes (no key, no egress)."""
        self._core = core
        self._mirrors = mirrors

    def generate(self, request: GenerateRequest, /) -> RelayedGenerateResponse:
        """Relay a generate call; mirror it unless the deployed logging drops its caller."""
        reply = self._core.generate(request)
        mirror_seq = None
        if reply.in_mirror:  # a call blinded by underreported_activity_logging is not mirrored
            call = MirroredCall(
                caller_identity=reply.caller_identity,
                turn_id=reply.turn_id,
                batch_id=reply.batch_id,
                credential_kind=reply.credential_kind,
                prompt_fp=input_fingerprint(request),
                output_fp=output_fingerprint(request, reply.text, reply.tool_calls),
                model_role=request.role,
            )
            mirror_log = self._mirrors.log_for(reply.episode_handle, reply.episode_id)
            mirror_seq = mirror_log.record(call.draft(reply.ts)).seq
        return RelayedGenerateResponse(**reply.model_dump(), mirror_seq=mirror_seq)

    def batch_generate(self, request: BatchGenerateRequest, /) -> RelayedBatchGenerateResponse:
        """Relay a batch call and mirror one record per prompt."""
        reply = self._core.batch_generate(request)
        mirror_log = self._mirrors.log_for(reply.episode_handle, reply.episode_id)
        mirror_seqs = tuple(
            mirror_log.record(
                MirroredCall(
                    caller_identity=request.caller_identity,
                    turn_id=None,
                    batch_id=reply.batch_id,
                    credential_kind="batch_id",
                    prompt_fp=fingerprint(prompt),
                    output_fp=fingerprint(text),
                    model_role=request.role,
                ).draft(reply.ts),
            ).seq
            for prompt, text in zip(request.prompts, reply.texts, strict=True)
        )
        return RelayedBatchGenerateResponse(**reply.model_dump(), mirror_seqs=mirror_seqs)

    def append_mirror(self, handle: str, append: MirrorAppend) -> Event:
        """Write one observable event the runner recorded (the mirror half of its dual record)."""
        return self._mirrors.log_for(handle, append.episode_id).record(append.draft)


class GatewayEdge(MirroringRelay):
    """The edge of one in-process episode: relays to its core, mirrors into its one mirror log.

    ``clock`` is unused (every mirror record carries the core's ts); it stays so that ``task.py`` and
    ``live.py`` still pass it.
    """

    def __init__(
        self,
        episode_id: str,
        core: CoreRelay,
        mirror_log: EventLog,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Wire the edge to its episode, its core, and the episode's mirror log."""
        del clock  # unused: see the class docstring
        super().__init__(core, EpisodeMirror(episode_id, mirror_log))


def create_edge_app(edge: MirroringRelay, *, control_key: SecretStr, settings: GatewaySettings) -> FastAPI:
    """Wire the edge onto a FastAPI app (what the agents point at); mirror writes need the control key.

    It serves no OpenAPI schema or docs: agents reach this app, and the schema would list its guarded route.
    """
    app = FastAPI(
        title="loc-arena gateway edge",
        openapi_url=None,
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=settings.max_request_bytes)],
    )
    mirror_writes = APIRouter(
        dependencies=[Depends(require_control_key(control_key, settings.control_key_header))],
    )

    @app.exception_handler(httpx.HTTPStatusError)
    def relay_core_refusal(request: Request, error: httpx.HTTPStatusError) -> Response:
        """Answer with the core's own status and body (409 no episode, 400 unknown role, 429 quota)."""
        refusal = error.response
        return Response(refusal.content, refusal.status_code, media_type=refusal.headers.get("content-type"))

    @app.get(HEALTH_ROUTE)
    def report_health() -> ServiceHealth:
        return ServiceHealth(ok=True)

    @app.post(GENERATE_ROUTE)
    def generate(request: GenerateRequest) -> RelayedGenerateResponse:
        return edge.generate(request)

    @app.post(BATCH_GENERATE_ROUTE)
    def batch_generate(request: BatchGenerateRequest) -> RelayedBatchGenerateResponse:
        return edge.batch_generate(request)

    @mirror_writes.post(MIRROR_EVENTS_ROUTE)
    def append_mirror(handle: EpisodeHandle, append: MirrorAppend) -> Event:
        try:
            return edge.append_mirror(handle, append)
        except ValueError as error:  # the handle's mirror belongs to another episode
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(error)) from error

    app.include_router(mirror_writes)
    return app


def build_edge_app() -> FastAPI:
    """The edge service for ``uvicorn --factory``: settings from the environment, key from /run/secrets."""
    settings = load_settings_from_environment().gateway
    control_key = load_container_secrets().control_key
    if control_key is None:
        raise RuntimeError("no control_key secret: the edge will not serve its mirror route unguarded")
    core = CoreClient(
        ServiceClient.connect(
            build_service_url(GATEWAY_CORE_HOSTNAME, settings.core_port),
            timeout_seconds=settings.relay_timeout_seconds,
        ),
    )
    edge = MirroringRelay(core, MirrorDirectory(MIRROR_MOUNT_PATH))
    return create_edge_app(edge, control_key=control_key, settings=settings)
