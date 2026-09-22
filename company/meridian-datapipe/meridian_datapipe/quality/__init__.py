"""Quality gates over documents and corpora, producing pass/fail reports with reasons."""

from __future__ import annotations

from meridian_datapipe.quality.gates import QualityGate, QualityReport, alpha_ratio

__all__ = [
    "QualityGate",
    "QualityReport",
    "alpha_ratio",
]
