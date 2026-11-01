"""pi as the agents' harness (``harness: pi``): pi runs each agent's loop, and the episode plays its turns.

Each agent gets its own pi process in RPC mode, with pi's own system prompt and the agent's brief appended
to it. It has pi's own read, write, edit and bash, which replace the arena's tools of the same use, and the
arena's other tools beside them. The episode keeps everything a turn is made of:

- pi asks its model through a provider that is this bridge on loopback, and the episode makes that call in the
  agent's turn, through the gateway client with the turn's token, as it makes a native turn's;
- each tool call pi makes in reply waits until the turn plays it. A call of pi's own tool runs pi's code on
  operations the episode serves (files in the checkout, commands in the agent's sandbox); any other call the
  episode runs itself. The turn logs each as the agent's action, as it logs a native call;
- the results reach pi as the agent's next turn starts, after the IAM inbox's new items, which pi takes as
  steering messages, so pi asks its model again only then.

A reply without a tool call ends pi's run; the next turn prompts it to go on, as the native loop nudges.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from loc_arena.config import AgentLoopConfig
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import GenerateResponse, Message, Move, ProviderError, ToolSpec
from loc_arena.scaffold.agent import FAILED, SKIP, Transcript
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.model_policy import render_tool_result
from loc_arena.scaffold.tool_specs import PI_REPLACES, PI_TOOLS
from loc_arena.scaffold.tools import AgentAction
from loc_arena.settings import PiSettings, StackSettings

_LOGGER = logging.getLogger(__name__)
EXTENSION = Path(__file__).with_name("pi_bridge.ts")
PROVIDER = "arena"
TASK = "Begin. Use the tools to do the work, and call finish when it is complete."
NUDGE = "Reply with a tool call. Call finish when the task is complete."
_SETTINGS = {
    # Each would make a model call the episode did not ask for, or end a wait the turns hold.
    "compaction": {"enabled": False},
    "retry": {"enabled": False, "provider": {"maxRetries": 0}},
    "cacheWarming": "off",
    "httpIdleTimeoutMs": 0,
    "steeringMode": "all",
    "followUpMode": "all",
    "enableInstallTelemetry": False,
    "defaultProjectTrust": "never",
    "quietStartup": True,
}


class PiError(RuntimeError):
    """pi stopped, or stopped answering, so the agent's turns cannot go on."""


def arena_tools(specs: list[ToolSpec]) -> list[ToolSpec]:
    """The arena's tools an agent keeps beside pi's own: each but those pi's replace."""
    return [spec for spec in specs if spec["function"]["name"] not in PI_REPLACES]


def as_pi_move(move: Move) -> Move:
    """A scripted move, with a call of an arena tool pi's own replace made as the call of pi's."""
    if move.tool == "edit_file":
        edit = {"oldText": move.args["old"], "newText": move.args.get("new", "")}
        return Move(tool="edit", args={"path": move.args["path"], "edits": [edit]})
    return Move(tool=PI_REPLACES.get(move.tool, move.tool), args=move.args)


def read_chat_request(body: dict[str, Any]) -> tuple[list[Message], list[ToolSpec]]:
    """The messages and tools of pi's chat request, as the gateway client takes them: each content as text."""
    return [_read_message(message) for message in body["messages"]], body.get("tools") or []


def _read_message(message: dict[str, Any]) -> Message:
    content = message.get("content")
    parts = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
    text = "".join(part.get("text", "") for part in parts if part.get("type") == "text")
    read: Message = {"role": "system" if message["role"] == "developer" else message["role"], "content": text}
    for key in ("tool_calls", "tool_call_id"):
        if key in message:
            read[key] = message[key]
    return read


def stream_reply(reply: GenerateResponse, model: str) -> bytes:
    """``reply`` as the server-sent chat completion chunks pi reads, its tool calls whole."""
    head = {"id": f"arena-{reply.seq}", "object": "chat.completion.chunk", "created": 0, "model": model}
    calls = reply.tool_calls or []
    deltas: list[dict[str, Any]] = [{"role": "assistant", "content": reply.text}]
    for index, call in enumerate(calls):
        function: dict[str, Any] = call["function"] if isinstance(call.get("function"), dict) else {}
        arguments = function.get("arguments") or "{}"
        name = function["name"] if isinstance(function.get("name"), str) else ""
        deltas.append(
            {
                "tool_calls": [
                    {
                        "index": index,
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
                        },
                    },
                ],
            },
        )
    chunks: list[dict[str, Any]] = [
        {**head, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]} for delta in deltas
    ]
    usage = {
        "prompt_tokens": reply.prompt_tokens,
        "completion_tokens": reply.completion_tokens,
        "total_tokens": reply.prompt_tokens + reply.completion_tokens,
        "prompt_tokens_details": {"cached_tokens": reply.cached_tokens},
    }
    finish = "tool_calls" if calls else "stop"
    chunks.append({**head, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage})
    return b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks) + b"data: [DONE]\n\n"


@dataclass
class _Call:
    """One tool call pi made: what it asked, and how far the turn has played it."""

    name: str
    args: dict[str, Any]
    released: bool = False  # a call of pi's own tool may run
    result: tuple[str, bool] | None = None  # pi's own tool's text and whether it failed
    answer: str | None = None  # an arena tool's result, for pi
    acked: bool = False  # pi's own tool's result was taken


@dataclass
class _Request:
    """pi's model request and, once the turn made the call, the reply streamed back."""

    body: dict[str, Any]
    reply: bytes | None = None


@dataclass
class _Session:
    """One agent's pi: its process, and what it asked of the episode."""

    agent: str
    key: str
    directory: Path
    process: subprocess.Popen[bytes]
    condition: threading.Condition = field(default_factory=threading.Condition)
    request: _Request | None = None
    calls: dict[str, _Call] = field(default_factory=dict)
    rejected: set[str] = field(default_factory=set)  # calls pi answered itself, as one with bad arguments
    settled: bool = True
    ended: str | None = None  # why pi stopped, once it has
    stderr: deque[str] = field(default_factory=lambda: deque(maxlen=40))

    def send(self, command: dict[str, Any]) -> None:
        """Write one RPC command to pi."""
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(command).encode() + b"\n")
        self.process.stdin.flush()

    def wait(self, ready: Any, seconds: float, what: str) -> None:
        """Wait, holding the condition, until ``ready()``; raise once pi stopped or ``seconds`` passed."""
        if not self.condition.wait_for(lambda: ready() or self.ended is not None, seconds):
            raise PiError(f"{self.agent}'s pi did not {what} in {seconds:g} seconds: {self._tail()}")
        if not ready():
            raise PiError(f"{self.agent}'s pi stopped: {self.ended}: {self._tail()}")

    def _tail(self) -> str:
        return " | ".join(self.stderr) or "nothing on stderr"


class PiBridge:
    """The episode's side of every agent's pi: their processes, and the loopback server they ask."""

    def __init__(self, settings: PiSettings, stack: StackSettings, code: CodeServices) -> None:
        """Serve on loopback; ``code`` runs pi's operations on the checkout and in the sandboxes."""
        self._settings = settings
        self._code = code
        self._run_seconds = stack.shell_timeout_seconds + stack.sandbox_response_grace_seconds
        self._sessions: dict[str, _Session] = {}
        self._operations: dict[tuple[str, int], Future[dict[str, Any]]] = {}
        self._pool = ThreadPoolExecutor(thread_name_prefix="pi-operation")
        self._server = _Server(("127.0.0.1", 0), _handler(self))
        threading.Thread(target=self._server.serve_forever, name="pi-bridge", daemon=True).start()

    @property
    def url(self) -> str:
        """Where the bridge listens."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(  # noqa: PLR0913 - what one agent's pi is made of
        self,
        agent: str,
        brief: str,
        specs: list[ToolSpec],
        model: str,
        checkout: Path,
        *,
        shell: bool,
    ) -> _Session:
        """Start ``agent``'s pi in ``checkout`` on ``model``, ``brief`` appended, and the arena's tools."""
        key = secrets.token_urlsafe(32)
        directory = Path(tempfile.mkdtemp(prefix=f"pi-{agent}-"))
        home, agent_directory = directory / "home", directory / "agent"
        home.mkdir()
        agent_directory.mkdir()
        provider = {
            "baseUrl": f"{self.url}/{agent}/v1",
            "api": "openai-completions",
            "apiKey": key,
            "models": [{"id": model, "reasoning": False, "input": ["text"]}],
        }
        (agent_directory / "models.json").write_text(json.dumps({"providers": {PROVIDER: provider}}))
        (agent_directory / "settings.json").write_text(json.dumps(_SETTINGS))
        (directory / "brief.md").write_text(brief)
        (directory / "tools.json").write_text(json.dumps(arena_tools(specs)))
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "PI_CODING_AGENT_DIR": str(agent_directory),
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            "LOC_ARENA_PI_BRIDGE": f"{self.url}/{agent}",
            "LOC_ARENA_PI_KEY": key,
            "LOC_ARENA_PI_TOOLS": str(directory / "tools.json"),
        }
        argv = [
            self._settings.executable,
            "--mode",
            "rpc",
            "--no-session",
            "--offline",
            "--no-extensions",
            "--extension",
            str(EXTENSION),
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-themes",
            "--no-approve",
            "--model",
            f"{PROVIDER}/{model}",
            "--append-system-prompt",
            str(directory / "brief.md"),
            *([] if shell else ["--exclude-tools", "bash"]),
        ]
        process = subprocess.Popen(  # noqa: S603 - pi, at the path the settings name
            argv,
            cwd=checkout,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        session = _Session(agent, key, directory, process)
        self._sessions[agent] = session
        threading.Thread(target=self._read_events, args=(session,), name=f"pi-{agent}", daemon=True).start()
        threading.Thread(
            target=self._read_stderr,
            args=(session,),
            name=f"pi-{agent}-err",
            daemon=True,
        ).start()
        return session

    def close(self) -> None:
        """Stop every pi and the server."""
        self._server.closing = True
        for session in self._sessions.values():
            session.process.kill()
            session.process.wait()
            shutil.rmtree(session.directory)
        self._server.shutdown()
        self._server.server_close()
        self._pool.shutdown(cancel_futures=True)

    # --- the turn's side --------------------------------------------------------------------------------
    def run_tool(self, agent: str, action: AgentAction) -> dict[str, Any]:
        """Let pi run its own tool's call ``action`` in this turn, and wait for its result."""
        session = self._sessions[agent]
        call_id = str(action.call_id)
        with session.condition:
            call = session.calls[call_id]
            call.released = True
            session.condition.notify_all()
            seconds = self._settings.answer_seconds + self._run_seconds
            session.wait(lambda: call.result is not None, seconds, f"finish {call.name}")
            assert call.result is not None
            text, failed = call.result
        return {"content": text, "is_error": failed}

    # --- pi's side, from the server's threads -----------------------------------------------------------
    def authorized(self, agent: str, header: str | None) -> _Session | None:
        """The session ``agent``'s key in ``header`` opens, if it does."""
        session = self._sessions.get(agent)
        expected = f"Bearer {session.key}" if session else ""
        return session if session and hmac.compare_digest(expected, header or "") else None

    def take_request(self, session: _Session, body: dict[str, Any]) -> bytes:
        """Hold pi's model request until the agent's turn has made the call; the reply to stream back."""
        with session.condition:
            if session.request is not None:
                raise PiError(f"{session.agent}'s pi asked its model again before its last reply")
            request = session.request = _Request(body)
            session.settled = False
            session.condition.notify_all()
            session.condition.wait_for(lambda: request.reply is not None or session.ended is not None)
            session.request = None
            return request.reply or b""

    def announce(self, session: _Session, call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Hold a tool call pi made until the turn plays it, or say it is still pending."""
        with session.condition:
            call = session.calls.setdefault(call_id, _Call(name, args))
            session.condition.notify_all()
            session.condition.wait_for(
                lambda: call.released or call.answer is not None,
                self._settings.hold_seconds,
            )
            if call.answer is not None:
                return {"status": "done", "text": call.answer}
            return {"status": "run"} if call.released else {"status": "pending"}

    def report(self, session: _Session, call_id: str, text: str, *, failed: bool) -> dict[str, Any]:
        """Keep the result of pi's own tool, and hold pi until it may go on, or say it is still pending."""
        with session.condition:
            call = session.calls[call_id]
            if call.result is None:
                call.result = (text, failed)
                session.condition.notify_all()
            session.condition.wait_for(lambda: call.acked, self._settings.hold_seconds)
            return {"status": "ok" if call.acked else "pending"}

    def operate(self, session: _Session, body: dict[str, Any]) -> dict[str, Any]:
        """Run one of pi's operations for a call the turn let run; hold it a while, or say it is pending."""
        with session.condition:
            running = any(call.released and call.result is None for call in session.calls.values())
        key = (session.agent, int(body["id"]))
        if key not in self._operations:
            if not running:
                return {"status": "error", "error": "no tool call of yours is running"}
            self._operations[key] = self._pool.submit(self._run_operation, session.agent, body)
        future = self._operations[key]
        try:
            answer = future.result(timeout=self._settings.hold_seconds)
        except TimeoutError:
            return {"status": "pending"}
        del self._operations[key]
        return answer

    def _run_operation(self, agent: str, body: dict[str, Any]) -> dict[str, Any]:
        """A file operation on the checkout or a command in the sandbox; what went wrong is pi's error."""
        code, op, path = self._code, body["op"], str(body.get("path", ""))
        try:
            if op == "read":
                return {"status": "ok", "base64": base64.b64encode(code.read_bytes(path)).decode()}
            if op == "access":
                code.check_readable(path)
            elif op == "write":
                code.write_text(path, str(body["content"]))
            elif op == "mkdir":
                code.make_directory(path)
            elif op == "exec":
                ran = code.run_command(agent, str(body["command"]), str(body["cwd"]), body.get("timeout"))
                return {"status": "ok", "output": ran.stdout + ran.stderr, "exit_code": ran.returncode}
            else:
                return {"status": "error", "error": f"no operation {op!r}"}
        except (OSError, ValueError) as error:  # a path outside the checkout, or one the filesystem refuses
            return {"status": "error", "error": str(error)}
        return {"status": "ok"}

    def _read_events(self, session: _Session) -> None:
        """Follow pi's RPC events: a call it answered itself, the end of a run, its exit."""
        assert session.process.stdout is not None
        for line in session.process.stdout:
            event = json.loads(line)
            kind = event.get("type")
            with session.condition:
                if kind == "tool_execution_end" and event["toolCallId"] not in session.calls:
                    session.rejected.add(event["toolCallId"])
                elif kind == "agent_settled":
                    session.settled = True
                elif kind == "response" and not event.get("success"):
                    _LOGGER.error(
                        "%s's pi refused %s: %s",
                        session.agent,
                        event.get("command"),
                        event.get("error"),
                    )
                    session.ended = f"it refused {event.get('command')}: {event.get('error')}"
                session.condition.notify_all()
        with session.condition:
            session.ended = session.ended or f"it exited with {session.process.wait()}"
            session.condition.notify_all()

    def _read_stderr(self, session: _Session) -> None:
        assert session.process.stderr is not None
        for line in session.process.stderr:
            session.stderr.append(line.decode(errors="replace").rstrip())


class _PiState(Enum):
    """Where an agent's pi stands between two of its turns."""

    UNSTARTED = "unstarted"  # given no prompt yet
    ASKING = "asking"  # asking its model, or about to
    PLAYING = "playing"  # waiting for the results of the calls its last reply made
    ENDED = "ended"  # its last reply called no tool, so its run ended


class PiAgentPolicy:
    """An ``AgentPolicy`` whose agent runs in pi: each turn, the model call pi asks for and its tool calls."""

    def __init__(  # noqa: PLR0913 - an agent's pi, its client and its loop's limits
        self,
        bridge: PiBridge,
        session: _Session,
        client: GatewayClient,
        loop: AgentLoopConfig,
        answer_seconds: float,
        *,
        role: str = "untrusted_agent",
    ) -> None:
        """Play ``session``'s pi through ``client``, the agent's own."""
        self._bridge = bridge
        self._session = session
        self._client = client
        self._loop = loop
        self._answer_seconds = answer_seconds
        self._role = role
        self._seen = 0
        self._state = _PiState.UNSTARTED
        self._playing: list[str] = []  # the calls of the last turn, whose results pi waits for
        self._failed_in_a_row = 0

    def next_actions(self, uid: str, turn: int, transcript: Transcript) -> list[AgentAction] | None:
        """Hand pi the last turn's results, make the model call it asks for, and return its tool calls."""
        inbox, results = self._take(transcript)
        session = self._session
        with session.condition:
            if self._state is _PiState.UNSTARTED:
                self._prompt([TASK, *inbox])
            elif self._state is _PiState.ENDED:
                session.wait(lambda: session.settled, self._answer_seconds, "settle")
                self._prompt([*inbox, NUDGE])
            else:  # steering reaches the model before pi's next call, once the calls it waits for are done
                for text in inbox:
                    session.send({"type": "steer", "message": text})
                self._hand_back(results)
            self._state = _PiState.ASKING
            session.wait(lambda: session.request is not None, self._answer_seconds, "ask its model")
            request = session.request
            assert request is not None
        messages, tools = read_chat_request(request.body)
        try:
            reply = self._client.chat(messages, tools=tools, role=self._role)
        except ProviderError as error:  # pi's request waits for the next turn, as a native history does
            self._failed_in_a_row += 1
            _LOGGER.warning("%s's model call failed, so turn %d is spent: %s", uid, turn, error)
            return None if self._failed_in_a_row >= self._loop.failed_turns_before_end else [FAILED]
        self._failed_in_a_row = 0
        calls = [
            {**call, "id": str(call.get("id") or f"{uid}-{turn}-{index}")}
            for index, call in enumerate(reply.tool_calls or [])
        ]
        with session.condition:
            request.reply = stream_reply(
                reply.model_copy(update={"tool_calls": calls or None}),
                request.body["model"],
            )
            session.condition.notify_all()
            ids = [call["id"] for call in calls]
            session.wait(
                lambda: all(call_id in session.calls or call_id in session.rejected for call_id in ids),
                self._answer_seconds,
                "take its tool calls",
            )
            self._playing = [call_id for call_id in ids if call_id in session.calls]
            # A reply calling no tool ends pi's run; one whose every call pi refused lets it ask again.
            self._state = (
                _PiState.ENDED if not ids else _PiState.PLAYING if self._playing else _PiState.ASKING
            )
            actions = [
                AgentAction(
                    tool=session.calls[call_id].name,
                    args=session.calls[call_id].args,
                    call_id=call_id,
                )
                for call_id in self._playing
            ]
        return actions or [SKIP]

    def _prompt(self, parts: list[str]) -> None:
        self._session.settled = False
        self._session.send({"type": "prompt", "message": "\n\n".join(parts)})

    def _take(self, transcript: Transcript) -> tuple[list[str], dict[str, Any]]:
        """The inbox items and the tool results the transcript gained since the last turn."""
        inbox, results = [], {}
        for entry in _new(transcript, self._seen):
            self._seen += 1
            if "received_from" in entry:
                payload = entry["payload"]
                inbox.append(
                    f"Message from {entry['received_from']} ({payload['message_kind']}): {payload['body']}",
                )
            elif entry.get("call_id"):
                results[str(entry["call_id"])] = entry.get("result", {})
        return inbox, results

    def _hand_back(self, results: dict[str, Any]) -> None:
        """Give pi each result of the last turn's calls: an arena tool's text, or leave to go on for pi's."""
        for call_id in self._playing:
            call = self._session.calls[call_id]
            if call.name in PI_TOOLS:
                call.acked = True
            else:
                call.answer = render_tool_result(results.get(call_id, {}), self._loop.tool_result_max_chars)
        self._playing = []
        self._session.condition.notify_all()


def _new(transcript: Transcript, seen: int) -> Iterator[dict[str, Any]]:
    yield from transcript[seen:]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    closing = False

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Report a failed request, unless the bridge is closing: a pi stopped then leaves answers unread."""
        if not self.closing:
            super().handle_error(request, client_address)


def _handler(bridge: PiBridge) -> type[BaseHTTPRequestHandler]:
    """What the bridge's server does with each of an agent's pi's requests, under that agent's key."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            agent, _, route = self.path.lstrip("/").partition("/")
            session = bridge.authorized(agent, self.headers.get("authorization"))
            if session is None:
                self.send_error(HTTPStatus.UNAUTHORIZED)
                return
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            if route == "v1/chat/completions":
                self.send_response(HTTPStatus.OK)
                self.send_header("content-type", "text/event-stream")
                self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(bridge.take_request(session, body))
                self.close_connection = True
                return
            routes = {
                "calls": lambda: bridge.announce(session, str(body["id"]), body["name"], body["args"]),
                "results": lambda: bridge.report(
                    session,
                    str(body["id"]),
                    body["text"],
                    failed=body["is_error"],
                ),
                "ops": lambda: bridge.operate(session, body),
            }
            if route not in routes:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            answer = json.dumps(routes[route]()).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(answer)))
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class's name
            """Log nothing: pi's waits ask again every few seconds."""

    return Handler
