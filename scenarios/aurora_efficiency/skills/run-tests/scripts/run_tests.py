"""Run one Meridian repo's tests from the workspace root and print a one-line JSON verdict.

Usage: ``python run_tests.py <repo>``. Every ``meridian-*`` repo of the workspace goes on ``PYTHONPATH``, as
the repos import one another; the script exits with pytest's exit code, or 2 for a repo not in the workspace.
The tests see only ``PATH``, ``HOME`` and ``TMPDIR`` of this environment, so no credential in it, and only
the end of their output is read, so a flood of it never fills memory.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TAIL_BYTES = 4096  # of the tests' output: room for pytest's summary, its last line
root = Path.cwd()
repos = sorted(path.name for path in root.glob("meridian-*") if path.is_dir())
repo = sys.argv[1] if len(sys.argv) > 1 else ""
if repo not in repos:
    print(json.dumps({"error": f"unknown repo {repo!r}; choices: {repos}"}))
    sys.exit(2)
environment = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR") if name in os.environ}
with tempfile.TemporaryFile() as output:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=root / repo,
        env={**environment, "PYTHONPATH": os.pathsep.join(str(root / name) for name in repos)},
        stdout=output,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output.seek(max(0, os.fstat(output.fileno()).st_size - TAIL_BYTES))
    lines = output.read().decode(errors="replace").strip().splitlines() or [""]
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
