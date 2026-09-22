"""Active data selection over teacher features."""

from __future__ import annotations

from meridian_distill.select.selector import (
    CRITERIA,
    Candidate,
    Selector,
    select_by_coverage,
    select_by_uncertainty,
)

__all__ = [
    "CRITERIA",
    "Candidate",
    "Selector",
    "select_by_coverage",
    "select_by_uncertainty",
]
