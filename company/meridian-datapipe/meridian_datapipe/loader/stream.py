"""A streaming loader that reads documents through a small bounded prefetch buffer.

The loader iterates any document iterable and keeps up to ``prefetch`` items staged ahead in a FIFO buffer, so
a consumer can pull items while the next few are already resolved. The buffer never reorders: documents are
yielded in exactly the source order, so the stream is deterministic. It also groups the stream into
fixed-size batches and supports bounded takes.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator

from meridian_datapipe.errors import IngestError
from meridian_datapipe.types import Document


class StreamingLoader:
    """Streams documents from a source iterable through a bounded, order-preserving prefetch buffer."""

    def __init__(self, source: Iterable[Document], *, prefetch: int = 4) -> None:
        """Hold the source iterable and the prefetch depth (at least one)."""
        if prefetch < 1:
            raise IngestError("prefetch must be >= 1", code="datapipe.ingest", prefetch=prefetch)
        self._source = source
        self._prefetch = prefetch

    @property
    def prefetch(self) -> int:
        """The number of documents staged ahead in the buffer."""
        return self._prefetch

    def __iter__(self) -> Iterator[Document]:
        """Yield documents in source order, keeping up to ``prefetch`` items staged in the buffer."""
        buffer: deque[Document] = deque()
        source = iter(self._source)
        exhausted = False
        while True:
            while not exhausted and len(buffer) < self._prefetch:
                try:
                    buffer.append(next(source))
                except StopIteration:
                    exhausted = True
            if not buffer:
                return
            yield buffer.popleft()

    def batches(self, size: int) -> Iterator[list[Document]]:
        """Yield the stream in fixed-size batches (the final batch may be smaller)."""
        if size < 1:
            raise IngestError("batch size must be >= 1", code="datapipe.ingest", size=size)
        batch: list[Document] = []
        for doc in self:
            batch.append(doc)
            if len(batch) == size:
                yield batch
                batch = []
        if batch:
            yield batch

    def take(self, n: int) -> list[Document]:
        """The first ``n`` documents of the stream (fewer if the source is shorter)."""
        if n < 0:
            raise IngestError("take count must be non-negative", code="datapipe.ingest", n=n)
        out: list[Document] = []
        for doc in self:
            if len(out) >= n:
                break
            out.append(doc)
        return out
