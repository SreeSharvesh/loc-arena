"""The IAM HTTP service: a thin, typed API over the core. Needs the ``service`` extra (FastAPI)."""

from __future__ import annotations

from meridian_iam.service.app import create_app

__all__ = ["create_app"]
