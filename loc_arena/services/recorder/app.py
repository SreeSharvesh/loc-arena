"""The recorder service: the only writer of the sealed logs (``/sealed/<handle>/*.jsonl``)."""

# No ``from __future__ import annotations``: FastAPI reads the route annotations at runtime.
import os
import threading
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

from fastapi import FastAPI, HTTPException

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
from loc_arena.stack.service_client import create_service_app
from loc_arena.stack.settings import GatewaySettings, load_settings_from_environment


class SealedEventLog:
    """One episode's sealed event log, and the fingerprint written at each of its seqs."""

    def __init__(self, path: Path, episode_id: str) -> None:
        """Append to ``path`` for ``episode_id``, recovering the fingerprints of the events already there."""
        self._log = AppendOnlyLog(path, episode_id)
        self._fingerprints = {event.seq: event.fp for event in read_events(path)} if path.exists() else {}
        self._lock = threading.Lock()

    def append(self, event: Event) -> str:
        """Fingerprint and append ``event``; an identical re-send writes nothing, acknowledged again."""
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
        """Append ``event`` to ``handle``'s log, bound to the episode of its first event; its fingerprint."""
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
    app = create_service_app("loc-arena recorder", settings.max_request_bytes)

    @app.get(HEALTH_ROUTE)
    def report_health() -> ServiceHealth:
        return ServiceHealth(ok=True)

    @app.post(RECORDER_EVENTS_ROUTE)
    def append_event(handle: EpisodeHandle, event: Event) -> AppendAck:
        try:
            fingerprint = sealed.append_event(handle, event)
        except ValueError as error:
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
