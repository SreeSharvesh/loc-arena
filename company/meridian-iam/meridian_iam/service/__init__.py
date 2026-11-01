"""The IAM HTTP service: a thin, typed API over the core. Needs the ``service`` extra (FastAPI)."""

from __future__ import annotations

from meridian_iam.service.app import TokenEndpoint, create_app

__all__ = ["TokenEndpoint", "create_app"]
