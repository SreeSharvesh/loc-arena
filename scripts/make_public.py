"""Build the curated PUBLIC tree (mentee repo) into ``dist/public-repo/`` and verify it excludes internals.

The public repo a mentee gets must NOT contain the phase prompts or the internal design/process docs. It
produces a verified public tree from the private repo by ALLOWLIST (copy only allowed top-level paths),
re-inits a clean git history there, and runs a DENYLIST verifier that FAILS if any excluded path (a prompt, an
internal doc, CLAUDE.md, the diagrams, or a secret) leaked in. Allowlist + verifier, not gitignore alone.

Run: ``python scripts/make_public.py`` (or ``make public-repo``). The verifier also runs as a test
(tests/integration/test_public_tree.py) so the exclusion guarantee is part of the gate.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

# The ONLY top-level paths that ship in the public tree (allowlist: an excluded path cannot leak).
ALLOWED_TOP: frozenset[str] = frozenset(
    {
        "loc_arena",
        "company",
        "configs",
        "scenarios",
        "tests",
        "scripts",
        "site",  # the GitHub Pages docs site (built in P14 CP5)
        "compose.yaml",
        "Makefile",
        "README.md",
        ".env.example",
        "pyproject.toml",
        "uv.lock",
        ".gitignore",
        ".pre-commit-config.yaml",
    }
)

# Never allowed in the public tree (denylist, enforced by the verifier over the allowlist too).
EXCLUDED_TOP: frozenset[str] = frozenset({"prompts", "docs", "CLAUDE.md", "diagrams"})
_SECRET_RE = re.compile(r"sk-or-[A-Za-z0-9]{8,}")  # an OpenRouter key must never appear


def _tracked_files(repo: Path) -> list[str]:
    proc = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True)
    out = proc.stdout
    return [line for line in out.splitlines() if line]


def _top(path: str) -> str:
    return path.split("/", 1)[0]


def build_public_tree(repo: Path, dest: Path) -> list[str]:
    """Copy the allowlisted tree from ``repo`` into ``dest`` (wiped first); return the copied paths."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    copied: list[str] = []
    # tracked files whose top-level component is allowlisted
    for rel in _tracked_files(repo):
        if _top(rel) not in ALLOWED_TOP:
            continue
        src = repo / rel
        if not src.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied.append(rel)
    # the site/ dir may be untracked at export time (built in CP5); include it if present
    site = repo / "site"
    if site.is_dir():
        for src in site.rglob("*"):
            if src.is_file():
                rel = str(src.relative_to(repo))
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                if rel not in copied:
                    copied.append(rel)
    return sorted(copied)


def verify_public_tree(dest: Path) -> list[str]:
    """Return every exclusion violation in ``dest`` (empty list == the public tree is clean)."""
    violations: list[str] = []
    for path in dest.rglob("*"):
        if path.is_dir() or ".git" in path.parts:
            continue
        rel = str(path.relative_to(dest))
        top = _top(rel)
        if top in EXCLUDED_TOP:
            violations.append(f"excluded path present: {rel}")
            continue
        if rel == "CLAUDE.md":
            violations.append("CLAUDE.md must not ship in the public tree")
        if path.name == ".env" or (path.suffix == ".env" and path.name != ".env.example"):
            violations.append(f"secret file present: {rel}")
        if path.suffix in (".py", ".md", ".yaml", ".yml", ".txt", ".html", ".cfg", ".toml", ".example"):
            try:
                if _SECRET_RE.search(path.read_text(errors="ignore")):
                    violations.append(f"possible provider key in {rel}")
            except OSError:
                continue
    return violations


def reinit_git(dest: Path) -> None:
    """Re-init a clean git history in the public tree (no private history carries over)."""
    if (dest / ".git").exists():
        shutil.rmtree(dest / ".git")
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "LOC-Arena public repo"],
        cwd=dest,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "loc-arena",
            "GIT_AUTHOR_EMAIL": "noreply@example.com",
            "GIT_COMMITTER_NAME": "loc-arena",
            "GIT_COMMITTER_EMAIL": "noreply@example.com",
            "PATH": _path(),
        },
    )


def _path() -> str:
    import os

    return os.environ.get("PATH", "")


def main() -> int:
    """Build dist/public-repo/, verify the exclusions, re-init git, and print a summary."""
    repo = Path(__file__).resolve().parents[1]
    dest = repo / "dist" / "public-repo"
    copied = build_public_tree(repo, dest)
    violations = verify_public_tree(dest)
    if violations:
        print("PUBLIC TREE VERIFIER FAILED:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        return 1
    reinit_git(dest)
    has_site = (dest / "site").is_dir()
    print(f"public tree built at {dest} ({len(copied)} files); exclusion verifier clean.")
    print(f"  site/ included: {has_site}" + ("" if has_site else "  (build it with P14 CP5 first)"))
    print("  NOTE: no LICENSE file exists in the repo; add one before publishing the public tree.")
    print("  to publish: push dist/public-repo/ to a new GitHub repo and enable Pages on the site/ dir.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
