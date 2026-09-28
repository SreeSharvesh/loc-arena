"""What can be tuned about the explorer's pages: how much of a run they carry and how they lay it out."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ExplorerSettings(BaseModel):
    """The explorer's tunables. The defaults suit a run of a few hundred agent turns."""

    model_config = ConfigDict(frozen=True)

    action_characters: int = Field(
        default=600,
        gt=0,
        description="How much of a tool call's most telling argument a transcript card carries; the card "
        "clamps it further on screen, so this only bounds the page size.",
    )
    list_page_size: int = Field(
        default=60,
        gt=0,
        description="How many steps the search list shows before it offers to show more.",
    )
    sparse_monitor_verdicts: int = Field(
        default=10,
        ge=0,
        description="A monitor with at most this many verdicts gets a narrow column in the transcript.",
    )
    read_probe_characters: int = Field(
        default=24,
        gt=0,
        description="The shortest stretch of plain text taken from a message to find it in a later prompt, "
        "which is how the page tells who read it; a message with no stretch this long is not traced.",
    )
    read_probe_cap: int = Field(
        default=60,
        gt=0,
        description="The longest stretch of a message searched for in later prompts.",
    )
