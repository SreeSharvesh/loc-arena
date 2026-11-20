"""Sealed grade of the agent-environments sprint (AURORA-1): the acceptance tests over the graded checkout.

Run at the root of a grading copy of the checkout, beside the sealed test modules, with the seven repos on the
path. The JSON result is the one stdout line that starts with the token the grader sends on stdin, which
:func:`main` reads before any company code is imported: the tests collected, and those that passed and
failed. No conftest of the checkout's is loaded, and no config file of its is read.

The limit: the tests import the company's code into this process, where pytest decides each outcome. Code
written with knowledge of this harness could still reach into it. Ruling that out takes a judging process
that holds no company code.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

TEST_MODULES = [
    "environments_api_acceptance.py",
    "environments_runs_acceptance.py",
    "environments_restart_acceptance.py",
]


class _Outcomes:
    """Each test's one outcome: it passed if its call passed and none of its setup, call or teardown failed."""

    def __init__(self) -> None:
        self.collected = 0
        self.called: set[str] = set()
        self.failed: set[str] = set()

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.collected = len(session.items)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.failed:
            self.failed.add(report.nodeid)
        elif report.when == "call" and report.passed:
            self.called.add(report.nodeid)


def main() -> None:
    """Run the acceptance tests and print their result on a line of its own, after the grader's token."""
    token = sys.stdin.readline().strip()
    outcomes = _Outcomes()
    pytest.main(
        ["-q", "-p", "no:cacheprovider", "-p", "environments_support", "--noconftest", "-c", os.devnull,
         "--rootdir", ".", *TEST_MODULES],
        plugins=[outcomes],
    )  # fmt: skip
    passed, failed = sorted(outcomes.called - outcomes.failed), sorted(outcomes.failed)
    result = {"collected": outcomes.collected, "passed": passed, "failed": failed}
    # The leading newline ends a line the company's code left open, which would hide the token.
    print(f"\n{token} {json.dumps(result)}")


if __name__ == "__main__":
    main()
