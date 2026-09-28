"""The recorder service: the only writer of the sealed logs, ``/sealed/<handle>/{events,model_calls}.jsonl``.

Reachable on sealed-net only, where the gateway core is its one client. It has no read route (only the
health check): the host copies the logs out through the evidence reader once the episode is over. The core
numbers each event; the recorder fingerprints it and appends it only if it continues the episode's log,
answering 409 on a non-increasing seq or another episode's event.

The core re-sends an event whose acknowledgement it lost (a transport error after the write). An identical
re-send of an event already written is acknowledged again with the same fingerprint and writes nothing, so
the core's numbering catches up instead of every later event meeting a 409. Different content at a written
seq is still refused. Model-call records carry no seq and are appended each time they are sent.

No ``from __future__ import annotations``: FastAPI reads the route annotations at runtime.
"""

import os
import threading
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

from fastapi import FastAPI, HTTPException
from starlette.middleware import Middleware
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from loc_arena.logging_.events import AppendOnlyLog, Event, read_events
from loc_arena.stack.constants import (
    EVENTS_FILE_NAME,
    HEALTH_ROUTE,
    MODEL_CALLS_FILE_NAME,
    RECORDER_EVENTS_ROUTE,
    RECORDER_MODEL_CALLS_ROUTE,
    SEALED_MOUNT_PATH,
)
from loc_arena.stack.contracts import AppendAck, EpisodeHandle, ModelCallRecord, ServiceHealth
from loc_arena.stack.settings import GatewaySettings, load_settings_from_environment


class SealedEventLog:
    """One episode's sealed event log, and the fingerprint written at each of its seqs."""

    def __init__(self, path: Path, episode_id: str) -> None:
        """Append to ``path`` for ``episode_id``, recovering the fingerprints of the events already there."""
        self._log = AppendOnlyLog(path, episode_id)
        self._fingerprints = {event.seq: event.fp for event in read_events(path)} if path.exists() else {}
        self._lock = threading.Lock()

    def append(self, event: Event) -> str:
        """Fingerprint and append ``event``, returning the fingerprint written for its seq.

        An identical re-send of an event already written returns that event's fingerprint and writes
        nothing. Otherwise raises ``ValueError`` on another episode's event or a seq not above the last
        one, the file unchanged. A fingerprint the event carries is ignored: the recorder computes its own.
        """
        unsigned = replace(event, fp="")
        with self._lock:
            written = self._fingerprints.get(unsigned.seq)
            if written is not None and written == unsigned.compute_fp():
                return written
            fingerprint = self._log.append(unsigned).fp
            self._fingerprints[unsigned.seq] = fingerprint
            return fingerprint


class SealedDirectory:
    """Every episode's sealed logs under one root, ``<root>/<handle>/`` (the routes validate each handle)."""

    def __init__(self, root: Path) -> None:
        """Keep the sealed logs under ``root`` (the sealed volume in the stack)."""
        self._root = root
        self._event_logs: dict[str, SealedEventLog] = {}
        self._lock = threading.Lock()

    def append_event(self, handle: str, event: Event) -> str:
        """Append ``event`` to ``handle``'s log, bound to the episode of its first event; its fingerprint.

        See :meth:`SealedEventLog.append`: an identical re-send is acknowledged again, anything else that
        does not continue the log raises ``ValueError``.
        """
        with self._lock:
            if handle not in self._event_logs:
                path = self._root / handle / EVENTS_FILE_NAME
                self._event_logs[handle] = SealedEventLog(path, event.episode_id)
            log = self._event_logs[handle]
        return log.append(event)

    def append_model_call(self, handle: str, record: ModelCallRecord) -> None:
        """Append one provider call to ``handle``'s model-call log, durably (fsync), under the lock."""
        path = self._root / handle / MODEL_CALLS_FILE_NAME
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(record.model_dump_json() + "\n")
                file.flush()
                os.fsync(file.fileno())


def create_recorder_app(sealed: SealedDirectory, *, settings: GatewaySettings) -> FastAPI:
    """Wire the recorder's two write routes and its health check onto a FastAPI app (no schema, no docs)."""
    app = FastAPI(
        title="loc-arena recorder",
        openapi_url=None,
        middleware=[Middleware(RequestBodyLimitMiddleware, max_body_size=settings.max_request_bytes)],
    )

    @app.get(HEALTH_ROUTE)
    def report_health() -> ServiceHealth:
        return ServiceHealth(ok=True)

    @app.post(RECORDER_EVENTS_ROUTE)
    def append_event(handle: EpisodeHandle, event: Event) -> AppendAck:
        try:
            fingerprint = sealed.append_event(handle, event)
        except ValueError as error:  # another episode, or a non-increasing seq that is no identical re-send
            raise HTTPException(status_code=HTTPStatus.CONFLICT, detail=str(error)) from error
        return AppendAck(fp=fingerprint)

    @app.post(RECORDER_MODEL_CALLS_ROUTE, status_code=HTTPStatus.NO_CONTENT)
    def append_model_call(handle: EpisodeHandle, record: ModelCallRecord) -> None:
        sealed.append_model_call(handle, record)

    return app


def build_recorder_app() -> FastAPI:
    """The recorder service for ``uvicorn --factory``: settings from the environment, logs on /sealed."""
    settings = load_settings_from_environment().gateway
    return create_recorder_app(SealedDirectory(SEALED_MOUNT_PATH), settings=settings)
