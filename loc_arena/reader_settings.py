"""The run reader's settings group, the run config's ``reader:`` block, beside the root settings."""

from __future__ import annotations

from pydantic import Field, PositiveFloat, PositiveInt

from loc_arena.service_settings import SettingsGroup


class ReaderSettings(SettingsGroup):
    """The run reader (``loc-arena read``, the config's ``reader:`` block)."""

    timeout_seconds: PositiveFloat = Field(
        default=1200.0,
        description="The longest one reading lasts; the reader is stopped there and has written no report.",
    )
    model_calls: PositiveInt = Field(
        default=150,
        description="Model calls the reader may make in one reading; the gateway refuses each one after.",
    )
    report_limit_bytes: PositiveInt = Field(
        default=1024 * 1024,
        description="The largest report a reading keeps; a larger one, or one that is no file, is refused.",
    )
    claim_limit: PositiveInt = Field(
        default=25,
        description="The most claims a reading keeps, which its brief states; each claim past it is dropped.",
    )
    claim_limit_characters: PositiveInt = Field(
        default=600,
        description="The longest claim a reading keeps; a longer one is dropped.",
    )
    quote_limit_characters: PositiveInt = Field(
        default=300,
        description="The longest quote of an agent's words a claim may hold; a claim with a longer one is "
        "dropped.",
    )
    word_fields: tuple[str, ...] = Field(
        default=(
            "body",
            "text",
            "title",
            "description",
            "comment",
            "message",
            "reason",
            "summary",
            "pr_title",
            "pr_body",
        ),
        description="The fields of an event's payload, and of its arguments, that hold words a claim may "
        "quote: what an agent wrote in a message, a ticket, a note or a pull request. No command or code is "
        "in such a field, so none is quoted.",
    )
    repeat_at_least: PositiveInt = Field(
        default=3,
        description="How often an agent runs one command before the taxonomy counts it a step repetition.",
    )
    outbound_proxy: str | None = Field(
        default=None,
        pattern=r"^[^\s:/]+:\d+$",
        description="The host:port a reading's gateway tunnels through, and where its relay listens, in "
        "place of gateway.outbound_proxy: an address apart from a play's lets a reading run while a play "
        "does. Unset: gateway.outbound_proxy.",
    )
