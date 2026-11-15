"""Check CHANGELOG.md: one release per merged pull request, in the format its head describes."""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TYPES = ("Added", "Changed", "Fixed", "Removed", "Security")
# The parts of the system a line may open with; the changelog's head lists the same names.
PARTS = (
    "agent",
    "run",
    "stack",
    "score",
    "monitor",
    "verifier",
    "grader",
    "record",
    "model gateway",
    "tools gateway",
    "sandbox",
    "runner",
    "IAM",
    "forge",
    "deploy service",
    "job service",
    "eval service",
    "agents service",
    "tickets service",
    "chat service",
    "notes service",
    "model store",
    "observability store",
    "run explorer",
    "run reader",
    "scenario",
    "engine",
    "docs",
    "repository",
)
MAX_TITLE_WORDS = 10
MAX_LINE_WORDS = 30
HEADING = re.compile(r"## \[(\d+)\] - (\d{4}-\d{2}-\d{2}): (\S.*)")
HEADING_SHAPE = "## [number] - YYYY-MM-DD: title"
OPENING = re.compile(
    r"(?:(?:The|A|An|Each|Every|All|No) )?("
    + "|".join(re.escape(part) for part in sorted(PARTS, key=len, reverse=True))
    + r")(?:e?s)?(?:'s|')?(?![\w-])",
    re.IGNORECASE,
)
MERGED = re.compile(r"\(#(\d+)\)$|^Merge pull request #(\d+) ")
HOW_TO_ADD = (
    "add a release for it at the top of CHANGELOG.md: the heading `## [<its number>] - <the day it merges, "
    "UTC>: <what the system now does>`, a type heading such as `### Changed`, then one line per change "
    "that opens with a part of the system"
)


@dataclass
class Release:
    """One pull request's release, as the file holds it."""

    number: int
    date: str
    row: int
    types: list[str] = field(default_factory=list)
    lines: list[tuple[str, str]] = field(default_factory=list)


def read_heading(row: int, raw: str) -> tuple[Release | None, list[str]]:
    """Read a release's heading, or say how to write it."""
    heading = HEADING.fullmatch(raw)
    if heading is None:
        return None, [f"line {row}: write the release heading as `{HEADING_SHAPE}`."]
    words = len(heading[3].split())
    problems = [f"line {row}: the title has {words} words; cut it to {MAX_TITLE_WORDS} at most."] * (
        words > MAX_TITLE_WORDS
    )
    problems += [f"line {row}: end the title without a full stop."] * heading[3].endswith(".")
    try:
        datetime.date.fromisoformat(heading[2])
    except ValueError:
        problems.append(f"line {row}: {heading[2]} is no date; write the merge day as YYYY-MM-DD.")
    return Release(int(heading[1]), heading[2], row), problems


def read_type(row: int, kind: str, release: Release) -> list[str]:
    """Open a type in a release, or say why it cannot be opened."""
    if kind not in TYPES:
        return [f"line {row}: `{kind}` is not a type; use one of {', '.join(TYPES)}."]
    if kind in release.types:
        return [f"line {row}: `{kind}` is twice in release {release.number}; merge the two."]
    release.types.append(kind)
    return []


def read_line(row: int, raw: str, release: Release) -> list[str]:
    """Read one row inside a release: a line of it, or text that has no place there."""
    if not raw.startswith("- "):
        return [
            f"line {row}: a release holds type headings and one-row `- ` lines only; move or cut this text.",
        ]
    kind = release.types[-1] if release.types else ""
    release.lines.append((kind, raw[2:]))
    problems = [] if kind else ["put this line under a type heading such as `### Changed`."]
    return [f"line {row}: {problem}" for problem in problems + line_problems(raw[2:])]


def read_head(row: int, raw: str) -> list[str]:
    """Check a row of the file's head: its list of parts must be the checker's."""
    stale = raw.startswith("Parts: ") and raw != "Parts: " + ", ".join(PARTS) + "."
    return [f"line {row}: list the parts as PARTS in scripts/check_changelog.py does."] if stale else []


def read_releases(text: str) -> tuple[list[Release], list[str]]:
    """Read every release, and say where the file's shape or a line's form breaks the format."""
    releases: list[Release] = []
    problems: list[str] = []
    for row, raw in enumerate(text.splitlines(), 1):
        if raw.startswith("## "):
            release, found = read_heading(row, raw)
            releases.extend([release] if release else [])
        elif not releases:
            found = read_head(row, raw)
        elif raw.startswith("### "):
            found = read_type(row, raw[4:], releases[-1])
        else:
            found = read_line(row, raw, releases[-1]) if raw.strip() else []
        problems.extend(found)
    problems.extend(
        f"line {release.row}: release {release.number} has no line; say what changed under a type heading."
        for release in releases
        if not release.lines
    )
    return releases, problems


def line_problems(line: str) -> list[str]:
    """Say what in one line of a release breaks the format."""
    problems = []
    prose = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
    words = len(prose.split())
    if words > MAX_LINE_WORDS:
        problems.append(f"the line has {words} words; cut it to {MAX_LINE_WORDS} at most.")
    spans = len(re.findall(r"`[^`]+`", line))
    if spans > 1:
        problems.append(f"the line holds {spans} code spans; keep the one a reader must type, or none.")
    if not prose.endswith("."):
        problems.append("end the line with a full stop.")
    if re.search(r"[.!?] +[A-Z]", re.sub(r"`[^`]+`", "", prose)):
        problems.append("the line holds more than one sentence; keep one.")
    if OPENING.match(line) is None:
        problems.append(f"open the line with the part of the system it is about, one of: {', '.join(PARTS)}.")
    return problems


def order_problems(releases: list[Release]) -> list[str]:
    """Say which releases are out of date order or twice in the file."""
    problems = [
        f"line {newer.row}: release {newer.number} ({newer.date}) is above the later "
        f"release {older.number} ({older.date}); put the newest release first."
        for newer, older in zip(releases, releases[1:], strict=False)
        if newer.date < older.date
    ]
    numbers = [release.number for release in releases]
    problems.extend(
        f"line {release.row}: release {release.number} is twice in the file; merge the two."
        for position, release in enumerate(releases)
        if release.number in numbers[:position]
    )
    return problems


def history_problems(releases: list[Release], merged: list[int], pull_request: int | None) -> list[str]:
    """Compare the releases with main's merged pull requests, newest first."""
    problems = []
    if pull_request is not None and pull_request not in merged and releases[0].number != pull_request:
        problems.append(f"pull request {pull_request} has no release; {HOW_TO_ADD}.")
    for position, release in enumerate(releases):
        mine = position == 0 and pull_request in (None, release.number)
        if release.number not in merged and not mine:
            problems.append(
                f"line {release.row}: {release.number} is not a pull request merged into main; "
                "only the top release may name the pull request being merged; fetch main if it merged since.",
            )
    return problems + missing_releases(releases, merged)


def missing_releases(releases: list[Release], merged: list[int]) -> list[str]:
    """Name each pull request merged since the file's oldest release that has no release."""
    numbers = {release.number for release in releases}
    oldest = releases[-1].number
    span = merged[: merged.index(oldest)] if oldest in merged else []
    return [
        f"pull request {number} is merged into main and has no release; add it where its merge day falls."
        for number in span
        if number not in numbers
    ]


def read_merged(main: str) -> list[int]:
    """Read the pull requests merged into main that this checkout holds, newest first."""
    try:
        base = subprocess.check_output(["git", "merge-base", main, "HEAD"], stderr=subprocess.PIPE)
    except (OSError, subprocess.CalledProcessError):
        base = b"HEAD"
    try:
        subjects = subprocess.check_output(
            ["git", "log", "--first-parent", "--format=%s", base.decode().strip()],
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    found = (MERGED.search(subject) for subject in subjects.decode().splitlines())
    return [int(match[1] or match[2]) for match in found if match]


def part_history(releases: list[Release], part: str) -> list[str]:
    """List one part's lines, newest first, each with its release."""
    return [
        f"[{release.number}] {release.date} {kind}: {line}"
        for release in releases
        for kind, line in release.lines
        if (opening := OPENING.match(line)) and opening[1].lower() == part.lower()
    ]


def main() -> int:
    """Check the changelog, or print one part's history from it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=Path("CHANGELOG.md"))
    parser.add_argument("--main", default="origin/main", help="the ref of main; defaults to origin/main")
    parser.add_argument("--pull-request", type=int, help="the pull request under check")
    parser.add_argument("--part", help="print this part's lines, newest first, and check nothing")
    arguments = parser.parse_args()
    releases, problems = read_releases(arguments.file.read_text(encoding="utf-8"))
    if arguments.part:
        print("\n".join(part_history(releases, arguments.part)))
        return 0
    if not releases:
        problems.append(f"the file holds no release; start one with `{HEADING_SHAPE}`.")
    else:
        problems.extend(order_problems(releases))
        merged = read_merged(arguments.main)
        if merged:
            problems.extend(history_problems(releases, merged, arguments.pull_request))
        elif arguments.pull_request is not None:
            problems.append(
                f"no merged pull request found from {arguments.main}; fetch main's whole history.",
            )
        else:
            print(f"No merged pull request found from {arguments.main}: releases not compared with main.")
    for problem in problems:
        print(f"{arguments.file}: {problem}")
    if problems:
        return 1
    print(f"Changelog checks passed ({len(releases)} releases).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
