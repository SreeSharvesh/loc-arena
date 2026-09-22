"""Core value types shared across the serving stack.

A :class:`Request` is one inference request (a prompt as a list of token ids, a generation budget, a priority,
and an arrival sequence for FIFO tie-breaking). A :class:`Response` is its produced tokens plus accounting. A
:class:`Batch` is a set of requests the scheduler runs together. All are immutable and dependency-free so
every
serving module agrees on the shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class Priority(IntEnum):
    """Request priority; higher runs first. FIFO breaks ties within a priority."""

    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass(frozen=True)
class Request:
    """One inference request: its id, prompt token ids, generation budget, priority, and arrival order."""

    request_id: str
    prompt: tuple[int, ...]
    max_tokens: int = 16
    priority: Priority = Priority.NORMAL
    arrival_seq: int = 0

    @property
    def prompt_len(self) -> int:
        """The number of prompt tokens."""
        return len(self.prompt)

    @property
    def total_len(self) -> int:
        """The maximum sequence length this request can reach (prompt plus generation budget)."""
        return len(self.prompt) + self.max_tokens


@dataclass(frozen=True)
class Response:
    """A produced response: the request id, the generated token ids, and per-request accounting."""

    request_id: str
    tokens: tuple[int, ...]
    prompt_len: int
    finish_reason: str = "length"
    cache_hits: int = 0

    @property
    def generated_len(self) -> int:
        """The number of generated tokens."""
        return len(self.tokens)


@dataclass(frozen=True)
class Batch:
    """A set of requests scheduled to run together, with the padded width the scheduler chose."""

    requests: tuple[Request, ...]
    padded_len: int
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def size(self) -> int:
        """The number of requests in the batch."""
        return len(self.requests)

    @property
    def padded_tokens(self) -> int:
        """The total padded token slots this batch occupies (``size * padded_len``)."""
        return self.size * self.padded_len

    @property
    def real_tokens(self) -> int:
        """The total real (unpadded) prompt tokens across the batch."""
        return sum(r.prompt_len for r in self.requests)
