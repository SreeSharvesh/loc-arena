"""What a chat starts with: Meridian's people, its channels and what was said in them and in direct messages.

A seed is replayed through the chat's own operations at the times it gives, in the order it lists them, so its
messages are the ones the chat would have stored had they been written live: each channel with its threads and
their replies, then the direct messages.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from meridian_controlplane.chat.store import CHANNEL_NAME_PATTERN, ChatStore


class _SeedModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SeedMessage(_SeedModel):
    """A message: who wrote it, when, and what it says."""

    by: str
    at: AwareDatetime
    body: str


class SeedThread(SeedMessage):
    """A message that starts a thread, and the replies in it, oldest first."""

    replies: list[SeedMessage] = []


class SeedChannel(_SeedModel):
    """A channel as it was created, and its threads, oldest first."""

    name: Annotated[str, Field(pattern=CHANNEL_NAME_PATTERN)]
    topic: str = ""
    created_by: str
    created_at: AwareDatetime
    threads: list[SeedThread] = []


class SeedDirectMessage(SeedMessage):
    """A direct message from one member to another."""

    to: str


class ChatSeed(_SeedModel):
    """Everyone besides the agents who is in the chat, its channels and the direct messages."""

    people: list[str]
    channels: list[SeedChannel]
    direct_messages: list[SeedDirectMessage] = []


def load_chat_seed(path: Path) -> ChatSeed:
    """The seed in the YAML file at ``path``."""
    return ChatSeed.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def open_seeded_store(database: Path, seed: ChatSeed, agents: Iterable[str]) -> ChatStore:
    """The store at ``database``, built from ``seed`` if there is none; ``agents`` are in the chat too.

    A new store is built aside and moved into place whole, so a replay that fails leaves no store to reopen.
    """
    members = [*seed.people, *agents]
    if not database.exists():
        database.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=database.parent) as building:
            built = Path(building) / database.name
            store = ChatStore(built, members)
            _replay(seed, store)
            store.close()
            built.replace(database)
    return ChatStore(database, members)


def _replay(seed: ChatSeed, store: ChatStore) -> None:
    for channel in seed.channels:
        store.create_channel(channel.created_by, channel.name, channel.topic, channel.created_at)
        for thread in channel.threads:
            started = store.post(thread.by, channel.name, thread.body, thread.at)
            for reply in thread.replies:
                store.reply(reply.by, started.id, reply.body, reply.at)
    for message in seed.direct_messages:
        store.send_dm(message.by, message.to, message.body, message.at)
