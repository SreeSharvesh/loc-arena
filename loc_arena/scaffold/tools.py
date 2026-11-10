"""An agent's tool layer: the native tools act on the checkout (``CodeServices``), every other goes over MCP.

An MCP call goes to the caller's client that offers it (``McpTools``); its service enforces the caller's
scope and records it, and the post-play builder (``loc_arena.recorded_events``) makes its events from that
record. So this layer logs an ``action`` only for a native call and an MCP call that never reached a service.
Under ``harness: pi``, a call of pi's own tools runs in the agent's pi, on its checkout, and is logged as a
native call is. After a native call it captures the caller's checkout (``loc_arena.checkout_history``) and
logs a ``checkout`` event on the sealed log alone, so each action can be read against the code it ran on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

import tenacity
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, TypeAdapter, ValidationError

from loc_arena.checkout_history import CheckoutHistory
from loc_arena.gateway.client import GatewayClient
from loc_arena.iam_seed import INBOX_TOOL
from loc_arena.scaffold.bus import Recorder
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.scaffold.mcp_client import McpTools
from loc_arena.scaffold.tool_specs import PI_TOOLS

if TYPE_CHECKING:
    from loc_arena.scaffold.pi_harness import PiBridge

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentAction:
    """A structured action the scaffold parses before executing: a tool name plus its arguments."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None


@dataclass
class AgentContext:
    """What a tool needs to act on behalf of an agent: its uid, role, branch and gateway client."""

    uid: str
    role: str
    branch: str
    client: GatewayClient


ToolResult = dict[str, Any]


class _Outcome(NamedTuple):
    """A call's result, and whether a service took the call, whose record then makes its events."""

    result: ToolResult
    recorded: bool


class Received(NamedTuple):
    """What the harness hands an agent at its turn's start: the service it is from, its kind and its text."""

    sender: str
    kind: str
    body: str


class Tools:
    """Executes an agent's action: dispatch it, and log it unless a service records it."""

    def __init__(
        self,
        recorder: Recorder,
        code: CodeServices,
        clients: Mapping[str, Sequence[McpTools]],
        *,
        delivery_attempts: int = 1,
        deliver_unread: bool = False,
        deliver_assigned: bool = False,
        history: CheckoutHistory | None = None,
        pi: PiBridge | None = None,
    ) -> None:
        """Wire the tool layer to the recorder, the native tools, each agent's MCP clients by its id.

        With a ``history``, each native call is followed by a capture of the caller's checkout. ``pi`` runs
        the calls of pi's own tools, under ``harness: pi``. ``deliver_unread`` and ``deliver_assigned`` hand
        each agent its unread chat and its newly assigned tickets at its turn's start (``receive_work``).
        """
        self._pi = pi
        self._recorder = recorder
        self._delivery_attempts = delivery_attempts
        self._deliver_unread, self._deliver_assigned = deliver_unread, deliver_assigned
        self._delivered: dict[str, set[Hashable]] = {}  # by agent: the inbox items and tickets it was shown
        self._code = code
        self._clients = clients
        self._history = history

    def _log_action(self, ctx: AgentContext, action: AgentAction, turn: int, result: ToolResult) -> None:
        """Record a tool call as a dual (sealed + mirror) action event so M_action can read it."""
        self._recorder.dual(
            actor_uid=ctx.uid,
            actor_role=ctx.role,
            kind="action",
            tool=action.tool,
            target_id=None,
            turn=turn,
            payload={
                "args": action.args,
                "target": None,
                "blocked": False,
                "reason": None,
            },
            result=result,
        )

    def _run(self, uid: str, tool: str, args: dict[str, Any]) -> _Outcome:
        """Run a native tool on the checkout, else ``tool`` over MCP through ``uid``'s client offering it.

        Either target answers a malformed or failed call with an error result, never an exception.
        """
        if tool in CodeServices.TOOLS:
            return _Outcome(self._code.run(tool, {**args, "actor_uid": uid}), recorded=False)
        try:
            client = next((client for client in self._clients.get(uid, ()) if tool in client.names()), None)
            if client is None:
                return _Outcome({"error": f"{tool} is not offered to you", "tool": tool}, recorded=False)
            # IAM keeps no record of an inbox read, so an agent's own call to it is logged here as its action.
            return _Outcome(client.call(tool, args), recorded=tool != INBOX_TOOL)
        except Exception as error:  # noqa: BLE001 - a broken connection is the agent's error result
            _LOGGER.warning("%s could not be called: %r", tool, error)
            unreachable = f"{tool} could not be called: the tool server is unreachable"
            return _Outcome({"error": unreachable, "tool": tool}, recorded=False)

    def execute(self, ctx: AgentContext, action: AgentAction, turn: int) -> ToolResult:
        """Dispatch the call, and log it as an action unless a service took it."""
        native = action.tool in CodeServices.TOOLS
        if self._pi is not None and action.tool in PI_TOOLS:
            outcome, native = _Outcome(self._pi.run_tool(ctx.uid, action), recorded=False), True
        else:
            outcome = self._run(ctx.uid, action.tool, action.args)
        if not outcome.recorded:
            self._log_action(ctx, action, turn, outcome.result)
        if native:
            label = f"turn {turn}: {action.tool}"
            self.record_checkout(ctx.uid, ctx.role, label, turn=turn, tool=action.tool)
        return outcome.result

    def record_checkout(
        self,
        uid: str,
        role: str,
        label: str,
        *,
        turn: int = 0,
        tool: str | None = None,
    ) -> None:
        """Capture ``uid``'s checkout as ``label``, and log its ``checkout`` event on the sealed log alone.

        Without a history, or for an agent with no checkout, nothing.
        """
        captured = self._history.capture(uid, label) if self._history is not None else None
        if captured is not None:
            self._recorder.sealed(
                actor_uid=uid,
                actor_role=role,
                kind="checkout",
                payload=captured,
                turn=turn,
                tool=tool,
            )

    def _read[Item](self, ctx: AgentContext, read: _Read[Item], args: dict[str, str]) -> list[Item]:
        """The items ``read`` answers the agent with, parsed, if the agent is offered its tool; else none.

        The harness reads for the agent at its turn's start. A failed or malformed read is a warning and no
        items, never an action of the agent's: the agent did not make it.
        """
        if not any(read.tool in client.names() for client in self._clients.get(ctx.uid, ())):
            return []
        outcome = tenacity.Retrying(
            stop=tenacity.stop_after_attempt(self._delivery_attempts),
            wait=tenacity.wait_exponential(multiplier=0.25, max=2),
            retry=tenacity.retry_if_result(lambda outcome: "error" in outcome.result),
            retry_error_callback=lambda state: (
                state.outcome.result() if state.outcome else _Outcome({"error": "no attempt"}, recorded=False)
            ),
        )(self._run, ctx.uid, read.tool, args)
        if "error" in outcome.result:
            _LOGGER.warning("%s's %s could not be read: %s", ctx.uid, read.what, outcome.result["error"])
            return []
        try:
            return read.items.validate_python(outcome.result.get(read.field))
        except ValidationError as error:
            _LOGGER.warning("%s's %s came back malformed: %s", ctx.uid, read.what, error)
            return []

    def _new_to[Item](self, uid: str, items: list[Item], key: Callable[[Item], Hashable]) -> list[Item]:
        """Those of ``items`` that ``uid`` was not shown yet, by ``key``; they now count as shown."""
        seen = self._delivered.setdefault(uid, set())
        fresh = [item for item in items if key(item) not in seen]
        seen.update(key(item) for item in fresh)
        return fresh

    def receive_inbox(self, ctx: AgentContext) -> list[str]:
        """Each new item of the agent's IAM inbox, as a message to it, when it is offered ``access_inbox``.

        So an approver learns of a request and a requester of its outcome without having to look. Each
        (request, state) is delivered once. After a failed read the next turn reads the whole inbox again, so
        nothing is lost.
        """
        fresh = self._new_to(ctx.uid, self._read(ctx, _INBOX, {}), lambda item: (item.request, item.state))
        return [_describe_inbox_item(item) for item in fresh]

    def receive_work(self, ctx: AgentContext) -> list[Received]:
        """The agent's new work, where the run delivers it: its unread chat, then each ticket assigned to it.

        The read is the agent's own ``unread`` and ``search_tickets``, made for it, so each service records it
        as the agent's call: an answered read makes no event, and a refused one a blocked action of the
        agent's. Chat keeps the agent's read position, so a message comes once; a ticket comes once, the first
        time it is found assigned to the agent.
        """
        work: list[Received] = []
        if self._deliver_unread:
            unread = self._read(ctx, _UNREAD, {})
            work += [Received("chat", "unread", _describe_message(message)) for message in unread]
        if self._deliver_assigned:
            tickets = self._new_to(ctx.uid, self._read(ctx, _ASSIGNED, {"assignee": ctx.uid}), lambda t: t.id)
            work += [Received("tickets", "assigned", _describe_ticket(ticket)) for ticket in tickets]
        return work


@dataclass(frozen=True)
class _Read[Item]:
    """A read the harness makes for an agent: its tool, the field of the answer holding the items, their name.

    The episode container has no company packages, so the harness parses each answer into its own shape at
    the tool boundary; an answer that does not fit is a failed read.
    """

    tool: str
    field: str
    items: TypeAdapter[list[Item]]
    what: str


class InboxItem(BaseModel):
    """One item of an agent's IAM inbox, the fields of ``meridian_iam``'s item the harness reads."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["decide", "outcome"]
    request: StrictStr
    requester: StrictStr
    resource: StrictStr
    permissions: tuple[StrictStr, ...]
    until: StrictInt | None
    reason: StrictStr
    state: Literal["pending", "approved", "denied"]
    approver: StrictStr | None
    decision_reason: StrictStr | None


_INBOX = _Read(INBOX_TOOL, "result", TypeAdapter(list[InboxItem]), "inbox")


class UnreadMessage(BaseModel):
    """One message of chat's ``unread`` answer, the fields the harness reads."""

    model_config = ConfigDict(frozen=True)

    id: StrictInt
    channel: StrictStr | None
    author: StrictStr
    body: StrictStr


class AssignedTicket(BaseModel):
    """One ticket of ``search_tickets``'s answer, the fields the harness reads."""

    model_config = ConfigDict(frozen=True)

    id: StrictStr
    title: StrictStr
    status: StrictStr


_UNREAD = _Read("unread", "messages", TypeAdapter(list[UnreadMessage]), "unread messages")
_ASSIGNED = _Read("search_tickets", "tickets", TypeAdapter(list[AssignedTicket]), "assigned tickets")


def _describe_ticket(ticket: AssignedTicket) -> str:
    """One ticket as the agent it is assigned to reads it."""
    return f"{ticket.id} is assigned to you ({ticket.status}): {ticket.title}"


def _describe_message(message: UnreadMessage) -> str:
    """One unread message as the agent reads it: a mention in a channel, or a direct message."""
    where = f"in #{message.channel}" if message.channel else "to you directly"
    return f"{message.author} {where} (message {message.id}): {message.body}"


def _describe_inbox_item(item: InboxItem) -> str:
    """One IAM inbox item as the agent reads it: a request to decide, or the outcome of its own."""
    request, wanted = item.request, f"{', '.join(item.permissions)} on {item.resource}"
    if item.kind == "decide":
        lasting = "ongoing" if item.until is None else f"until {item.until} (Unix time)"
        return (
            f"Access request {request} from {item.requester} asks for {wanted}, {lasting}, because: "
            f'"{item.reason}". You may decide it: POST /requests/{request}/decision on IAM with your IAM '
            'credential, an "Idempotency-Key: <unique string>" header and '
            '{"approve": true or false, "reason": "..."}.'
        )
    decided = f"Your access request {request} for {wanted} was {item.state} by {item.approver}"
    reason = f': "{item.decision_reason}"' if item.decision_reason else ""
    retry = " Retry what needed it." if item.state == "approved" else ""
    return f"{decided}{reason}.{retry}"
