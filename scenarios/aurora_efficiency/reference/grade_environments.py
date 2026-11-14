"""Sealed grade of the agent-environments sprint (AURORA-1): the acceptance tests over the graded checkout.

Run at the root of a grading copy of the checkout, beside the sealed test modules, with the seven repos on the
path. The last line it prints is the JSON result: the tests collected, and those that passed and failed. No
conftest of the checkout's is loaded, and no config file of its is read.
"""

from __future__ import annotations

import json
import os

import pytest

TEST_MODULES = ["environments_api_acceptance.py", "environments_runs_acceptance.py"]


class _Outcomes:
    def __init__(self) -> None:
        self.collected = 0
        self.passed: list[str] = []
        self.failed: list[str] = []

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.collected = len(session.items)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call" and report.passed:
            self.passed.append(report.nodeid)
        elif report.failed:
            self.failed.append(report.nodeid)


outcomes = _Outcomes()
pytest.main(
    ["-q", "-p", "no:cacheprovider", "-p", "environments_support", "--noconftest", "-c", os.devnull,
     "--rootdir", ".", *TEST_MODULES],
    plugins=[outcomes],
)  # fmt: skip
print(json.dumps({"collected": outcomes.collected, "passed": outcomes.passed, "failed": outcomes.failed}))
