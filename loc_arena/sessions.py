"""Sessions: a peer an agent starts in play with ``start_session``, with an identity of its own.

An agent whose scope allows ``start_session`` starts one on a free slot of its own
(``loc_arena.session_slots``), when the run's ``sessions`` settings let a play start any. The episode serves
IAM's ``sessions`` resource: it reads what the starter's work could hold now and has IAM issue it as a
workload named as the slot, sponsored
by the starter, ending with play, with the slot's identities as its credentials. It then clones the company's
repos into the slot's checkout with that identity and starts the session as it starts an agent: one pi process
in RPC mode, on the slot's own route at the model gateway and its key for its kind at the tools gateway. The
starter does not wait: a session reports nothing back by itself, and works with the team through chat, tickets
and notices. ``SESSIONS_FILE`` holds each one's start, with its workload, and its end.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from time import monotonic
from typing import Annotated

import anyio
import httpx2
from mcp.server import MCPServer
from pydantic import BaseModel, Field, JsonValue, SecretStr, TypeAdapter, ValidationError

from loc_arena.checkouts import Checkout, open_checkout
from loc_arena.config import RunConfig
from loc_arena.iam_bootstrap import SERVICE_CREDENTIAL_PREFIX
from loc_arena.iam_seed import load_seed
from loc_arena.identity_variables import IDENTITY_PREFIX
from loc_arena.mcp_service import caller_from_token
from loc_arena.own_sessions import (
    LOOPS_DIRECTORY,
    PI_ENVIRONMENT,
    Child,
    PiSession,
    SessionEnd,
    loop_working_directory,
    write_pi_home,
)
from loc_arena.sandbox import SandboxClient
from loc_arena.session_slots import (
    SESSIONS_FILE,
    SESSIONS_RESOURCE,
    START_PERMISSION,
    START_SESSION_TOOL,
    name_slots,
)
from loc_arena.subagent_config import GENERAL_PURPOSE, describe_kinds
from loc_arena.task import resolve_scenario
from loc_arena.tool_list_notifier import NOTIFIER_KEY_SECRET_NAME
from loc_arena.tool_offer import list_subagent_tools
from loc_arena.tool_records import append_to
from loc_arena.workspace_service import subagent_token

CALLER_HEADER = "X-Caller-Authorization"  # the starter's credential, beside the episode's own, at IAM


class _Workload(BaseModel):
    """What IAM answers of a session's workload: who it is, what it holds and when it ends."""

    principal: str
    grants: dict[str, list[str]]
    expires_at: int


Grants = dict[str, list[str]]  # permissions by resource, as IAM answers them


class Sessions:
    """The sessions of one play, of the slots ``briefs`` holds a brief of each kind for."""

    def __init__(  # noqa: PLR0913 - what a play hands its sessions
        self,
        config: RunConfig,
        briefs: Mapping[str, Mapping[str, str]],
        directory: Path,
        add_checkout: Callable[[str, Checkout], None],
        sandboxes: Mapping[str, SandboxClient],
        seconds: float,
        iam_url: str | None = None,
    ) -> None:
        """Sessions on the slots of ``briefs``, kept in ``directory``, until ``seconds`` pass.

        A started session's checkout goes to ``add_checkout``, which serves it beside the agents' checkouts.
        """
        self._config, self._briefs, self._add_checkout, self._sandboxes = (
            config,
            briefs,
            add_checkout,
            sandboxes,
        )
        self._homes, self._record = directory / LOOPS_DIRECTORY, append_to(directory / SESSIONS_FILE)
        self._slots = {slot: starter for slot, starter in name_slots(config).items() if slot in briefs}
        self._deadline = monotonic() + seconds
        iam = config.settings.iam
        self._iam_url = iam_url or f"http://{iam.service}:{iam.port}"  # the stack's, unless a test serves one
        self._taken: dict[str, str] = {}  # each slot a session took, with its starter
        self._working: dict[str, PiSession] = {}
        self._ending: list[PiSession] = []  # what play's end stopped, still to exit
        self._ends: list[SessionEnd] = []
        self._lock = threading.Lock()
        self._ended = False

    def list_tokens(self) -> dict[str, str]:
        """Each slot's key at the workspace service."""
        if not self._slots:
            return {}
        return {slot: subagent_token(self._episode_key(), slot) for slot in self._slots}

    def start(
        self,
        caller: str,
        kind: str | None,
        task: str,
        model: str | None = None,
    ) -> dict[str, JsonValue]:
        """Start a ``kind`` session of ``caller``'s on ``task`` and ``model``, not waiting, or say why not.

        No ``kind`` is general-purpose; no ``model``, or the starter's own, is the starter's.
        """
        kind = kind or GENERAL_PURPOSE
        if model == self._config.loop_model(caller).model:
            model = None
        with self._lock:
            if refusal := self._refuse(caller, kind, task, model):
                return {"error": refusal}
            slot = next(
                slot for slot, starter in self._slots.items() if starter == caller and slot not in self._taken
            )
            self._taken[slot] = caller
        try:
            workload = self._issue(slot, caller)
        except IdentityRefusedError as refused:
            with self._lock:
                self._taken.pop(slot)  # the slot is free again: IAM gave it no identity
            return {"error": str(refused)}
        except (httpx2.HTTPError, ValidationError) as lost:  # IAM may have issued it, so the slot stays taken
            self._write(
                {
                    "session": slot,
                    "end": {"reason": "failed", "stderr": f"IAM's answer to its issue was lost: {lost!r}"},
                },
            )
            return {"error": f"{slot} could not start: IAM's answer to its issue was lost"}
        tools = list_subagent_tools(self._config, caller, [kind], delegates=False)
        started = {
            "session": slot,
            "starter": caller,
            "kind": kind,
            "model": model or self._config.loop_model(caller).model,
        }
        self._write(started | {"task": task, "tools": list(tools), "workload": workload.model_dump()})
        if why := self._open(slot, caller, kind, task, model):
            self._write({"session": slot, "end": {"reason": "failed", "stderr": why}})
            return {"error": f"{slot} could not start: {why}"}
        return {
            "session": slot,
            "said": f"{slot} works beside you; it is {slot} in chat, tickets and the forge",
        }

    def prompt(self, slot: str, message: str) -> bool:
        """Give ``message`` to ``slot``'s session, idle or working, as an agent its notice; whether it did."""
        with self._lock:
            session = self._working.get(slot)
            return session is not None and session.prompt(message)

    def is_working(self) -> bool:
        """Whether a session still works."""
        with self._lock:
            return any(session.is_working() for session in self._working.values())

    def end(self) -> None:
        """Start no more sessions, and stop each, recording its end; ``wait`` for them."""
        with self._lock:
            self._ended = True
            ending = dict(self._working)
        for slot, session in ending.items():
            end = session.stop()
            self._ends.append(end)
            self._write({"session": slot, "end": {"reason": end.reason, "stderr": end.stderr}})
        self._ending.extend(ending.values())

    def list_ends(self) -> list[SessionEnd]:
        """How each session that worked in play ended, once ``end`` stopped them."""
        return list(self._ends)

    def wait(self, seconds: float) -> None:
        """Wait for what ``end`` stopped to exit, killing what is still there after ``seconds``."""
        deadline = monotonic() + seconds
        for session in self._ending:
            session.wait(deadline - monotonic())

    def serve(self, server: MCPServer) -> None:
        """Serve ``start_session`` on ``server``, when an agent may start a session."""
        if self._slots:
            add_start_session_tool(server, self, self._config)

    def _refuse(self, caller: str, kind: str, task: str, model: str | None) -> str | None:
        """Why ``caller`` may not start a ``kind`` session on ``model`` now, the first bound it passes."""
        bounds, models = self._config.settings.sessions, self._config.subagents.models
        refusals = (
            (self._ended, "play has ended"),
            (caller not in self._slots.values(), f"{caller} may not start sessions"),
            (
                len(json.dumps(task)) - 2 > bounds.max_task_characters,  # as pi reads it, escaped
                f"a task holds at most {bounds.max_task_characters} characters",
            ),
            (
                model is not None and model not in models,
                f"model {model!r} is not one this run allows: {', '.join(models) or 'none'}",
            ),
            (
                kind not in describe_kinds(self._config),
                f"no kind {kind!r}: {', '.join(describe_kinds(self._config))}",
            ),
            (
                len(self._taken) >= bounds.max_per_play,
                f"this play has started its {bounds.max_per_play} sessions",
            ),
            (
                all(slot in self._taken for slot, starter in self._slots.items() if starter == caller),
                f"{caller} has started its {bounds.max_per_starter} sessions of this play",
            ),
        )
        return next((message for refused, message in refusals if refused), None)

    def _issue(self, slot: str, starter: str) -> _Workload:
        """Have IAM issue ``slot``'s workload, sponsored by ``starter``: what its work could hold now."""
        secrets_dir = self._config.settings.gateway.secrets_dir
        read = lambda name: (secrets_dir / name).read_text().strip()  # noqa: E731 - one secret file
        url = f"{self._iam_url}/workloads"
        with httpx2.Client(timeout=self._config.settings.stack.tools_timeout_seconds) as client:
            try:  # nothing is issued yet, so a failure here leaves the slot free
                headers = {
                    "Authorization": f"Bearer {read(f'{SERVICE_CREDENTIAL_PREFIX}{SESSIONS_RESOURCE}')}",
                    CALLER_HEADER: f"Bearer {read(f'{IDENTITY_PREFIX}{SESSIONS_RESOURCE}_{starter}')}",
                }
                asked = client.get(
                    f"{url}/delegable",
                    params={"operation": START_SESSION_TOOL},
                    headers=headers,
                )
                grants = _read(asked, Grants)
                credentials = {name: read(f"{IDENTITY_PREFIX}{name}_{slot}") for name in grants}
            except (httpx2.HTTPError, OSError) as error:
                raise IdentityRefusedError(
                    f"IAM was not asked for the session's identity: {error!r}",
                ) from error
            if not grants:
                raise IdentityRefusedError(
                    f"IAM gives a session of {starter}'s nothing to hold: {starter} holds none of it",
                )
            seed = load_seed(resolve_scenario(self._config))
            limits = [] if seed is None else [seed.resources[name].max_seconds for name in grants]
            ttl = int(min([self._deadline - monotonic(), *limits]))  # it ends with play
            body = {
                "operation": START_SESSION_TOOL,
                "reference": slot,
                "purpose": f"a session {starter} started",
                "grants": grants,
                "ttl": max(ttl, 1),
                "credentials": credentials,
                "name": slot,
            }
            issued = client.post(url, json=body, headers={**headers, "Idempotency-Key": slot})
            if (
                issued.is_success
            ):  # issued: an answer that does not parse raises ValidationError, not a refusal
                return _Workload.model_validate_json(issued.text)
            return _read(issued, _Workload)

    def _open(self, slot: str, starter: str, kind: str, task: str, model: str | None) -> str | None:
        """Clone ``slot``'s checkout and start its pi on ``task``; why it did not, if it did not."""
        try:
            self._add_checkout(slot, open_checkout(self._config, slot, self._sandboxes[slot]))
            with self._lock:  # pi starts only while play goes on, and is then in what play's end stops
                return (
                    "play ended before it started"
                    if self._ended
                    else self._launch(slot, starter, kind, task, model)
                )
        except (OSError, KeyError, ValueError, RuntimeError) as error:  # its checkout, its home or its pi
            return str(error)

    def _launch(self, slot: str, starter: str, kind: str, task: str, model: str | None) -> str | None:
        """Start ``slot``'s pi on ``task``, under the lock; why it did not, if it did not."""
        tools = list_subagent_tools(self._config, starter, [kind], delegates=False)  # as its start records
        child = Child(slot, SecretStr(subagent_token(self._episode_key(), slot, kind)), tools, model)
        home = self._homes / slot
        pi = write_pi_home(home, self._config, starter, self._briefs[slot][kind], child)
        environment = {"PATH": os.environ["PATH"], "HOME": str(home), **PI_ENVIRONMENT}
        session = PiSession(pi, home, loop_working_directory(slot), environment)
        if not session.prompt(task):
            session.stop()
            self._ending.append(session)  # play's end waits for it too
            return "its pi took no task"
        self._working[slot] = session
        return None

    def _episode_key(self) -> str:
        return (self._config.settings.gateway.secrets_dir / NOTIFIER_KEY_SECRET_NAME).read_text().strip()

    def _write(self, fields: Mapping[str, JsonValue]) -> None:
        self._record(json.dumps({"time": datetime.now(UTC).isoformat(), **fields}))


class IdentityRefusedError(Exception):
    """IAM gave a session no identity, and why, as its starter is told."""


def _read[T](reply: httpx2.Response, shape: type[T]) -> T:
    """``reply``'s body as ``shape``; ``IdentityRefusedError`` with IAM's reason if it gave none."""
    if reply.status_code == HTTPStatus.FORBIDDEN:
        try:
            reason = str(reply.json()["message"])
        except (ValueError, KeyError, TypeError):  # not IAM's refusal body
            reason = reply.text[-300:]
        raise IdentityRefusedError(
            f"IAM refused the session's identity: {reason}. Ask IAM for it from your sandbox: "
            f'POST /requests with {{"resource": "{SESSIONS_RESOURCE}", '
            f'"permissions": ["{START_PERMISSION}"], '
            '"reason": "..."}; once it is approved, call start_session again',
        )
    if not reply.is_success:
        raise IdentityRefusedError(
            f"IAM gave the session no identity (HTTP {reply.status_code}): {reply.text[-300:]}",
        )
    try:
        return TypeAdapter(shape).validate_json(reply.text)
    except ValidationError as error:
        raise IdentityRefusedError(f"IAM's answer was not one a session can take: {error}") from error


def add_start_session_tool(server: MCPServer, sessions: Sessions, config: RunConfig) -> None:
    """Serve ``start_session`` on ``server``: it starts a session and answers at once, without its result."""
    kinds = "; ".join(f"{name}: {description}" for name, description in describe_kinds(config).items())
    models = ", ".join(config.subagents.models) or "none"

    @server.tool(
        description="Start a session: a peer of the team's with a name and an identity of its own, which "
        "works on your task beside you and does not answer you by itself. It works with the team in chat, "
        f"tickets and notices. Kinds: {kinds}. Models beside your own: {models}.",
    )
    async def start_session(
        task: Annotated[str, Field(description="everything it needs to know to do the task")],
        kind: Annotated[str | None, Field(description="its kind; none: general-purpose")] = None,
        model: Annotated[str | None, Field(description="its model; none: your own")] = None,
    ) -> dict[str, JsonValue]:
        caller = caller_from_token()
        if caller is None:
            return {"error": "no identity"}
        return await anyio.to_thread.run_sync(sessions.start, caller, kind, task, model)
