"""Run one Meridian repo's tests from the workspace root and print a one-line JSON verdict.

Usage: ``python run_tests.py <repo>``. Every ``meridian-*`` repo of the workspace goes on ``PYTHONPATH``, as
the repos import one another; the script exits with pytest's exit code, or 2 for a repo not in the workspace.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main(arguments: list[str]) -> int:
    """Run the tests of the repo named first in ``arguments`` and print the verdict; return the exit code."""
    root = Path.cwd()
    repos = sorted(path.name for path in root.glob("meridian-*") if path.is_dir())
    repo = arguments[0] if arguments else ""
    if repo not in repos:
        print(json.dumps({"error": f"unknown repo {repo!r}; choices: {repos}"}))
        return 2
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(str(root / name) for name in repos)}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=root / repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = result.stdout.strip().splitlines() or result.stderr.strip().splitlines()[-1:] or [""]
    print(
        json.dumps(
            {
                "repo": repo,
                "passed": result.returncode == 0,
                "returncode": result.returncode,
                "summary": lines[-1],
            },
        ),
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
