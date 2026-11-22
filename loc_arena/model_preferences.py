"""What a run config asks of a model call beside its model: OpenRouter's provider routing and reasoning."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr


class ProviderPreferences(BaseModel):
    """OpenRouter provider routing preferences for a model call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order: list[StrictStr] | None = Field(
        default=None,
        description="List of provider slugs to try in priority order.",
    )
    allow_fallbacks: StrictBool | None = Field(
        default=None,
        description="Whether to allow backup providers when prioritized providers are unavailable.",
    )


class ReasoningPreferences(BaseModel):
    """OpenRouter reasoning settings for a model call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    effort: Literal["max", "xhigh", "high", "medium", "low", "minimal", "none"] = Field(
        description="Reasoning effort level ('none' disables reasoning).",
    )
