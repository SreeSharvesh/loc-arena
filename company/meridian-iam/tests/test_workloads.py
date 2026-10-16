"""Workload identities: what a service may delegate, what a workload gets, and when it stops."""

from __future__ import annotations

import pytest

from meridian_iam.contracts import Invalid
from meridian_iam.core import Iam


@pytest.mark.parametrize(
    ("issuer", "delegable"),
    [
        ("missing", {"notes": ["read"]}),
        ("notes", {"missing": ["read"]}),
        ("notes", {"notes": ["fly"]}),
    ],
)
def test_delegating_an_unknown_resource_or_permission_is_invalid(
    iam: Iam,
    issuer: str,
    delegable: dict[str, list[str]],
) -> None:
    with pytest.raises(Invalid):
        iam.allow_delegation(issuer, delegable)
