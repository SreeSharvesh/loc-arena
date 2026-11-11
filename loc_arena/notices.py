"""Notices: what tells an agent's session that something it subscribed to happened.

An agent subscribes to topics with its tools (``add_notice_tools``, served with its workspace tools) and
starts with ``pi.subscriptions``. The topics: ``dm``, a direct message to it;
``channel:<name>``, a message in that channel; ``job``, a change of state of a job of its own; ``ticket``, a
ticket assigned to it; ``repo:<name>``, a pull request opened or merged in that repo.

A notice comes from a record a live service wrote (``loc_arena.tool_records``): the episode reads each
service's new lines from the records volume and builds their events as the monitors' are built after play
(``loc_arena.recorded_events``). A notice is one line saying that something happened: what it holds is for
the agent to read with its tools. An agent gets none for what it did itself, and none it did not subscribe to.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field, JsonValue

from loc_arena.config import RunConfig
from loc_arena.mcp_service import caller_from_token
from loc_arena.recorded_events import RecordedEvent, record_line_parser

OWN_TOPICS = ("dm", "job", "ticket")  # topics about the subscriber alone
NAMED_TOPICS = ("channel", "repo")  # topics of one channel or repo: ``channel:<name>``
NOTICE_TOOLS = ("subscribe", "unsubscribe", "list_subscriptions")  # served with the workspace tools
_TOPICS = (
    "dm (a direct message to you), job (a job of yours changed state), ticket (a ticket was assigned to "
    "you), channel:<name> (a message in that channel), repo:<name> (a pull request opened or merged in that "
    "repo)"
)
_ASSIGNING_TOOLS = frozenset({"create_ticket", "edit_ticket"})


@dataclass(frozen=True)
class Notice:
    """One line for ``agent``, subscribed to ``topic``: something happened."""

    agent: str
    topic: str
    text: str


def describe(event: RecordedEvent) -> Iterator[tuple[str, str | None, str]]:
    """Each topic ``event`` is of, the one agent it is for (none: every subscriber), and its notice's text."""
    actor, payload = event.actor_uid, event.payload
    if event.kind == "message" and payload["dm"]:
        yield "dm", event.target_id, f"{actor} sent you a direct message."
    elif event.kind == "message" and isinstance(payload["channel"], str):
        # A reply over HTTP names its message alone, so its record has no channel to notify.
        yield f"channel:{payload['channel']}", None, f"{actor} wrote in the channel {payload['channel']}."
    elif event.kind == "job":
        yield "job", actor, f"Your job {payload['name']} ({payload['job_id']}) is now {payload['state']}."
    elif event.kind == "pr":
        done = "merged" if payload.get("merged") else "opened"
        repo = payload["target_repo"]
        yield f"repo:{repo}", None, f"{actor} {done} pull request {payload['number']} in {repo}."
    elif event.tool in _ASSIGNING_TOOLS and not payload["blocked"]:
        assignee = payload["args"].get("assignee")
        if isinstance(assignee, str):
            yield "ticket", assignee, f"{actor} assigned a ticket to you."


class Notices:
    """Each agent's subscriptions, and the notices the services' new records make for them."""

    def __init__(self, config: RunConfig, agents: Iterable[str]) -> None:
        """Each of ``agents`` starts subscribed to ``pi.subscriptions``; records so far make no notice."""
        self._lock = threading.Lock()
        self._topics = {agent: set(config.settings.pi.subscriptions) for agent in agents}
        directory = config.settings.stack.records_directory
        self._parsers = {
            directory / f"{service.name}.jsonl": record_line_parser(service, config)
            for service in config.live_services
            if service.tools or service.rights
        }
        self._read = {path: path.stat().st_size if path.exists() else 0 for path in self._parsers}

    def subscribe(self, agent: str, topic: str) -> list[str]:
        """Subscribe ``agent`` to ``topic``; its topics after. Raises ``ValueError`` for no topic."""
        kind, _, name = topic.partition(":")
        if topic not in OWN_TOPICS and not (kind in NAMED_TOPICS and name):
            forms = [*OWN_TOPICS, *(f"{kind}:<name>" for kind in NAMED_TOPICS)]
            raise ValueError(f"no topic {topic!r}: a topic is one of {', '.join(forms)}")
        with self._lock:
            self._topics[agent].add(topic)
        return self.list_topics(agent)

    def unsubscribe(self, agent: str, topic: str) -> list[str]:
        """End ``agent``'s subscription to ``topic``, if it has one; its topics after."""
        with self._lock:
            self._topics[agent].discard(topic)
        return self.list_topics(agent)

    def list_topics(self, agent: str) -> list[str]:
        """The topics ``agent`` is subscribed to."""
        with self._lock:
            return sorted(self._topics[agent])

    def read(self) -> list[Notice]:
        """The notices of every record line written since the last read, in each service's order."""
        return [notice for event in self._read_events() for notice in self._notify(event)]

    def _read_events(self) -> Iterator[RecordedEvent]:
        for path, parse in self._parsers.items():
            if not path.exists():
                continue
            with path.open("rb") as records:
                records.seek(self._read[path])
                written = records.read()
            whole = written[: written.rfind(b"\n") + 1]  # a line still being written is read next time
            self._read[path] += len(whole)
            for line in whole.decode(errors="replace").splitlines():
                try:
                    record = parse(line, tuple(self._topics))
                except ValueError:  # no record (pydantic's ValidationError is one): it makes no notice
                    continue
                yield from record.events if record is not None else []

    def _notify(self, event: RecordedEvent) -> Iterator[Notice]:
        for topic, only, text in describe(event):
            with self._lock:
                subscribed = [agent for agent, topics in self._topics.items() if topic in topics]
            for agent in subscribed:
                # A job changes state by itself; anything else an agent did is no news to it.
                if only in (None, agent) and (event.kind == "job" or agent != event.actor_uid):
                    yield Notice(agent, topic, text)


def add_notice_tools(server: MCPServer, notices: Notices) -> None:
    """Add to ``server``, which names its caller, the tools an agent manages its subscriptions with."""

    def call(change: str, topic: str) -> dict[str, JsonValue]:
        agent = caller_from_token()
        if agent is None:
            raise ValueError("no identity: subscriptions belong to authenticated callers")
        return {"subscribed": [*getattr(notices, change)(agent, topic)]}

    @server.tool(
        description="Subscribe to a topic. When something happens in it you get a one-line notice, at any "
        f"time, and read what happened with your other tools. Topics: {_TOPICS}.",
    )
    def subscribe(topic: Annotated[str, Field(description="the topic")]) -> dict[str, JsonValue]:
        return call("subscribe", topic)

    @server.tool(description="End your subscription to a topic: you get no more notices of it.")
    def unsubscribe(topic: Annotated[str, Field(description="the topic")]) -> dict[str, JsonValue]:
        return call("unsubscribe", topic)

    @server.tool(description="List the topics you are subscribed to.")
    def list_subscriptions() -> dict[str, JsonValue]:
        agent = caller_from_token()
        return {"subscribed": [*notices.list_topics(agent)] if agent else []}
