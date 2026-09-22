"""An append-only, versioned results store.

:class:`ResultsStore` appends :class:`~meridian_evalkit.store.models.EvalRun` records and hands back a
monotonic, one-based version for each. Runs are retrieved by version or by run id, the latest version is
always available, and any two versions can be diffed. The store never mutates or drops a stored run, so a
version always resolves to the same record.
"""

from __future__ import annotations

from meridian_evalkit.errors import StoreError, VersionNotFoundError
from meridian_evalkit.store.diff import RunDiff, diff_runs
from meridian_evalkit.store.models import EvalRun


class ResultsStore:
    """Holds eval runs in append order and retrieves them by version or run id."""

    def __init__(self) -> None:
        """Start empty."""
        self._runs: list[EvalRun] = []

    def append(self, run: EvalRun) -> int:
        """Append ``run`` and return its one-based version."""
        self._runs.append(run)
        return len(self._runs)

    def __len__(self) -> int:
        """The number of stored versions."""
        return len(self._runs)

    @property
    def latest_version(self) -> int:
        """The most recent version (raises if the store is empty)."""
        if not self._runs:
            raise StoreError("results store is empty", code="evalkit.store")
        return len(self._runs)

    def versions(self) -> tuple[int, ...]:
        """Every stored version, in ascending order."""
        return tuple(range(1, len(self._runs) + 1))

    def get(self, version: int) -> EvalRun:
        """The run stored at ``version`` (raises :class:`VersionNotFoundError` if out of range)."""
        if version < 1 or version > len(self._runs):
            raise VersionNotFoundError(
                "no such results-store version",
                code="evalkit.store.version_not_found",
                version=version,
            )
        return self._runs[version - 1]

    def latest(self) -> EvalRun:
        """The most recently appended run (raises if the store is empty)."""
        return self.get(self.latest_version)

    def versions_for(self, run_id: str) -> tuple[int, ...]:
        """Every version whose run has id ``run_id``, in ascending order."""
        return tuple(v for v in self.versions() if self.get(v).run_id == run_id)

    def diff(self, left_version: int, right_version: int) -> RunDiff:
        """Diff the runs at ``left_version`` and ``right_version``."""
        return diff_runs(self.get(left_version), self.get(right_version))
