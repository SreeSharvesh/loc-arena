"""Datapipe-specific errors, extending the shared Meridian hierarchy.

The data platform raises these so callers can tell an ingest/schema fault from a tokenizer fault from a dedup,
contamination, sharding, or quality fault. They all derive from :class:`meridian_common.errors.MeridianError`,
so a generic handler still catches them, while the stable ``code`` keys alerts on the specific stage.
"""

from __future__ import annotations

from meridian_common.errors import MeridianError, ValidationError


class IngestError(ValidationError):
    """A raw source record could not be read or normalized into a :class:`Document`."""

    code = "datapipe.ingest"


class TokenizeError(MeridianError):
    """A tokenizer was given input it cannot encode or a token stream it cannot decode."""

    code = "datapipe.tokenize"


class DedupError(MeridianError):
    """A dedup pass was given inconsistent inputs (e.g. a threshold out of range)."""

    code = "datapipe.dedup"


class ContaminationError(MeridianError):
    """The contamination filter or its holdout registry was built or queried inconsistently."""

    code = "datapipe.contamination"


class ShardError(MeridianError):
    """A document could not be assigned to a shard, or a manifest lookup failed."""

    code = "datapipe.shard"


class QualityError(MeridianError):
    """A quality gate was configured with contradictory bounds."""

    code = "datapipe.quality"
