from __future__ import annotations

import pytest

from meridian_jobsvc.accountant import Ledger, Quota, Spend
from meridian_jobsvc.errors import QuotaExceeded


def test_charge_accumulates_spend() -> None:
    ledger = Ledger()
    ledger.charge("team-a", compute=2.0)
    ledger.charge("team-a", compute=3.0, inference=1.0)
    spent = ledger.spent("team-a")
    assert spent.compute == 5.0
    assert spent.inference == 1.0


def test_unset_identity_has_zero_spend() -> None:
    assert Ledger().spent("nobody") == Spend()


def test_quota_enforced_and_ledger_unchanged_on_reject() -> None:
    ledger = Ledger()
    ledger.set_quota("team-a", compute=5.0)
    ledger.charge("team-a", compute=4.0)
    with pytest.raises(QuotaExceeded):
        ledger.charge("team-a", compute=2.0)
    # The rejected charge left spend untouched.
    assert ledger.spent("team-a").compute == 4.0


def test_inference_quota_independent_of_compute() -> None:
    ledger = Ledger()
    ledger.set_quota("t", compute=None, inference=2.0)
    ledger.charge("t", compute=100.0, inference=2.0)  # compute uncapped
    with pytest.raises(QuotaExceeded):
        ledger.charge("t", inference=0.5)


def test_negative_charge_rejected() -> None:
    with pytest.raises(QuotaExceeded):
        Ledger().charge("t", compute=-1.0)


def test_remaining_reports_headroom() -> None:
    ledger = Ledger()
    ledger.set_quota("t", compute=10.0, inference=4.0)
    ledger.charge("t", compute=6.0, inference=1.0)
    remaining = ledger.remaining("t")
    assert remaining.compute == 4.0
    assert remaining.inference == 3.0


def test_remaining_uncapped_is_none() -> None:
    ledger = Ledger()
    ledger.charge("t", compute=5.0)
    assert ledger.remaining("t") == Quota(compute=None, inference=None)


def test_report_is_sorted_snapshot_and_total() -> None:
    ledger = Ledger()
    ledger.charge("b", compute=1.0)
    ledger.charge("a", compute=2.0, inference=1.0)
    report = ledger.report()
    assert list(report) == ["a", "b"]
    total = ledger.total()
    assert total.compute == 3.0
    assert total.inference == 1.0
