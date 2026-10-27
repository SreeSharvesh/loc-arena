"""The chat service: Meridian's chat, with channels, their threads and direct messages.

``store`` holds them in SQLite, ``seed`` what a chat starts with, and ``service`` the HTTP API and the MCP
tools over a store. ``Authority``, shared with the tickets service, is what a deployment hands in to decide
every call.
"""

from __future__ import annotations

from meridian_controlplane.chat.seed import ChatSeed, load_chat_seed, open_seeded_store
from meridian_controlplane.chat.service import SERVICE, build_chat_server, create_chat_api
from meridian_controlplane.chat.store import Channel, ChatError, ChatStore, Message
from meridian_controlplane.tickets.authority import Authority

__all__ = [
    "SERVICE",
    "Authority",
    "Channel",
    "ChatError",
    "ChatSeed",
    "ChatStore",
    "Message",
    "build_chat_server",
    "create_chat_api",
    "load_chat_seed",
    "open_seeded_store",
]
