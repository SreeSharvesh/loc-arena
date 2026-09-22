"""Process-local cost accounting for the platform pipeline.

A lightweight, in-process meter of unit work by kind -- tokens processed, dedup comparisons, teacher calls,
padded token-slots served, reference-embedding recomputes -- so a run's cost can be observed and compared
across changes. Recording is side-effect-only: it never changes a computation's result, so it is safe to leave
on in the hot path. Counters are reset between runs.

The running totals live in a closure rather than in module globals, so they are read only through
:func:`total` and :func:`snapshot` (there is no module-level accumulator to poke), and each caller binds
:func:`record` at import. This keeps the meter's readings a faithful account of the work the primitives did.
"""

from __future__ import annotations

from collections.abc import Callable


def _make() -> tuple[
    Callable[[], None],
    Callable[..., None],
    Callable[[], int],
    Callable[[], dict[str, int]],
]:
    """Build the meter over closure-held state; returns its ``(reset, record, total, snapshot)`` functions."""
    work = [0]
    by_kind: dict[str, int] = {}

    def reset() -> None:
        """Zero the meter (called at the start of each measured run)."""
        work[0] = 0
        by_kind.clear()

    def record(kind: str, n: int = 1) -> None:
        """Charge ``n`` units of work of ``kind`` to the current run."""
        work[0] += n
        by_kind[kind] = by_kind.get(kind, 0) + n

    def total() -> int:
        """The total units of work recorded since the last :func:`reset`."""
        return work[0]

    def snapshot() -> dict[str, int]:
        """A copy of the per-kind work counts recorded since the last :func:`reset`."""
        return dict(by_kind)

    return reset, record, total, snapshot


reset, record, total, snapshot = _make()

__all__ = ["record", "reset", "snapshot", "total"]
