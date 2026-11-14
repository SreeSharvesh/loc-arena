"""The text the loop finder reads of the chat requests and replies in the gateway's call record."""

from __future__ import annotations

from loc_arena.verifier.chat_text import read_prompt, read_reply
from pydantic import JsonValue


def test_a_reply_whose_tool_calls_are_null_keeps_its_content() -> None:
    reply: JsonValue = {
        "choices": [{"message": {"role": "assistant", "content": "Next: the cache.", "tool_calls": None}}],
    }

    assert read_reply(reply) == "Next: the cache."


def test_a_request_replaying_a_message_whose_tool_calls_are_null_keeps_every_message() -> None:
    request: JsonValue = {
        "messages": [
            {"role": "user", "content": "Go."},
            {"role": "assistant", "content": "Done.", "tool_calls": None},
        ],
    }

    assert read_prompt(request) == "Go.\nDone."
