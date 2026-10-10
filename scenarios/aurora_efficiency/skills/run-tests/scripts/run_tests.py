"""Run one Meridian repo's tests from the workspace root and print a one-line JSON verdict.

Usage: ``python run_tests.py <repo>``. Every ``meridian-*`` repo of the workspace goes on ``PYTHONPATH``, as
the repos import one another; the script exits with pytest's exit code, or 2 for a repo not in the workspace.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

root = Path.cwd()
repos = sorted(path.name for path in root.glob("meridian-*") if path.is_dir())
repo = sys.argv[1] if len(sys.argv) > 1 else ""
if repo not in repos:
    print(json.dumps({"error": f"unknown repo {repo!r}; choices: {repos}"}))
    sys.exit(2)
result = subprocess.run(
    [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
    cwd=root / repo,
    env={**os.environ, "PYTHONPATH": os.pathsep.join(str(root / name) for name in repos)},
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
sys.exit(result.returncode)
