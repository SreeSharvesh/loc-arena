"""One agent's code tools over a company checkout: the execution app's engine, and STACK=0's local runner.

The tools are an allowlist (``CodeToolName``): read, write, edit, list and search files, run a repository's
suite, run the directional benchmark, and a real ``bash``. Every path argument is confined to the checkout
(``..``, an absolute path, or a symlink resolving outside it is refused), every result is capped by
``settings.execution``, and a malformed call or a failing file operation comes back as an error result,
never an exception: a live model routinely writes bad arguments.

The shell is off unless the workspace is built with ``shell_enabled=True``, which only the execution app
does: inside an agent's sandbox. A workspace on the host (STACK=0) answers ``bash`` with an error result
and starts no process, so no agent gets a shell next to the machine's credentials.

The benchmark reports the company's own inline cost accounting as honest, directional feedback; it never
reads or moves the sealed grade meter.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError

from loc_arena.execution.checkout import (
    IGNORED_NAMES,
    Checkout,
    copy_repositories,
    extract_last_line,
    list_repositories,
)
from loc_arena.execution.command import run_command
from loc_arena.stack.contracts import BashResult, CodeToolCall, CodeToolName, CodeToolResult
from loc_arena.stack.settings import ExecutionSettings

ToolOutput = dict[str, JsonValue]
SHELL_DISABLED_ERROR: Final = "bash only runs in an agent's sandbox; this workspace has no shell"
_JSON_OBJECT: Final = TypeAdapter(ToolOutput)


class ToolArguments(BaseModel):
    """A tool's arguments as the model wrote them: immutable; an argument the tool lacks is ignored."""

    model_config = ConfigDict(frozen=True)


class PathArguments(ToolArguments):
    """``read_file`` and ``list_dir``: a path under the checkout (its root by default)."""

    path: str = ""


class WriteFileArguments(ToolArguments):
    """``write_file``: the file and its whole new content."""

    path: str
    content: str = ""


class EditFileArguments(ToolArguments):
    """``edit_file``: replace every occurrence of ``old`` with ``new``."""

    path: str
    old: str
    new: str = ""


class SearchArguments(ToolArguments):
    """``search_code`` and ``grep``: a regular expression, searched in the ``.py`` files under ``path``."""

    pattern: str
    path: str = ""


class RunTestsArguments(ToolArguments):
    """``run_tests``: the repository whose suite runs."""

    repo: str = ""


class BashArguments(ToolArguments):
    """``bash``: one command line."""

    command: str


class Workspace:
    """Runs one agent's allowlisted code tools over a checkout: a ``CodeToolRunner``."""

    def __init__(
        self,
        checkout: Checkout,
        settings: ExecutionSettings,
        *,
        shell_enabled: bool = False,
    ) -> None:
        """Serve ``checkout`` with the limits of ``settings``; ``bash`` runs only if ``shell_enabled``."""
        self._checkout = checkout
        self._root = checkout.root.resolve()
        self._settings = settings
        self._shell_enabled = shell_enabled
        self._tools: Mapping[CodeToolName, Callable[[ToolOutput], ToolOutput]] = {
            "read_file": self._read_file,
            "write_file": self._write_file,
            "edit_file": self._edit_file,
            "list_dir": self._list_dir,
            "search_code": self._search_code,
            "grep": self._search_code,
            "run_tests": self._run_tests,
            "run_benchmark": self._run_benchmark,
            "profile": self._run_benchmark,
            "bash": self._bash,
        }

    def run(self, call: CodeToolCall, /) -> CodeToolResult:
        """Run one allowlisted tool call; bad arguments or a failed file operation give an error result."""
        try:
            result = self._tools[call.tool](call.arguments)
        except ValueError as error:  # pydantic's ValidationError, a path escaping the checkout, bad UTF-8
            result = {"error": f"bad args for {call.tool}: {error}", "tool": call.tool}
        except OSError as error:  # e.g. writing over a directory, or a file the sandbox user cannot read
            result = {"error": f"{call.tool} failed: {error}", "tool": call.tool}
        return CodeToolResult(result=result)

    def seed(self, source: Path) -> None:
        """Copy the company repositories of ``source`` into the checkout, unless it holds some already.

        Idempotent, so every sandbox sharing the checkout may seed it: the first finds it empty, the others
        leave the agents' work alone. Two concurrent seeds write the same files (``dirs_exist_ok``).
        """
        if not list_repositories(self._root):
            copy_repositories(source, self._root, list_repositories(source))

    def _resolve(self, relative: str) -> Path:
        """``relative`` under the checkout; one resolving outside (``..``, absolute, symlink) is refused."""
        try:
            path = (self._root / relative).resolve()
        except RuntimeError as error:  # a symlink loop (Python 3.12 raises RuntimeError for it)
            raise ValueError(f"cannot resolve {relative}: {error}") from error
        if not path.is_relative_to(self._root):
            raise ValueError(f"path escapes the checkout: {relative}")
        return path

    def _read_file(self, arguments: ToolOutput) -> ToolOutput:
        parsed = PathArguments.model_validate(arguments)
        path = self._resolve(parsed.path)
        if not path.is_file():
            return {"error": "not a file", "path": parsed.path}
        limit = self._settings.max_read_characters
        with path.open(encoding="utf-8", errors="replace") as file:
            text = file.read(limit + 1)  # one character past the cap tells truncation, and no more is read
        return {"path": parsed.path, "content": text[:limit], "truncated": len(text) > limit}

    def _write_file(self, arguments: ToolOutput) -> ToolOutput:
        parsed = WriteFileArguments.model_validate(arguments)
        path = self._resolve(parsed.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(parsed.content, encoding="utf-8")
        return {"path": parsed.path, "bytes_written": len(parsed.content.encode())}

    def _edit_file(self, arguments: ToolOutput) -> ToolOutput:
        parsed = EditFileArguments.model_validate(arguments)
        path = self._resolve(parsed.path)
        if not path.is_file():
            return {"error": "not a file", "path": parsed.path}
        text = path.read_text(encoding="utf-8")
        count = text.count(parsed.old)
        if count == 0:
            return {"error": "old string not found", "path": parsed.path, "replaced": 0}
        path.write_text(text.replace(parsed.old, parsed.new), encoding="utf-8")
        return {"path": parsed.path, "replaced": count}

    def _list_dir(self, arguments: ToolOutput) -> ToolOutput:
        parsed = PathArguments.model_validate(arguments)
        path = self._resolve(parsed.path)
        if not path.is_dir():
            return {"error": "not a directory", "path": parsed.path}
        entries: list[JsonValue] = [
            {"name": child.name, "type": "dir" if child.is_dir() else "file"}
            for child in sorted(path.iterdir())
            if child.name not in IGNORED_NAMES
        ]
        return {"path": parsed.path, "entries": entries}

    def _search_code(self, arguments: ToolOutput) -> ToolOutput:
        parsed = SearchArguments.model_validate(arguments)
        try:
            expression = re.compile(parsed.pattern)
        except re.error as error:
            return {"error": f"bad regex: {error}"}
        matches: list[JsonValue] = []
        for file in sorted(self._resolve(parsed.path).rglob("*.py")):
            relative = file.relative_to(self._root)
            if IGNORED_NAMES.intersection(relative.parts) or not self._holds(file):
                continue
            try:
                lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, 1):
                if expression.search(line):
                    text = line.strip()[: self._settings.max_match_line_characters]
                    matches.append({"file": str(relative), "line": number, "text": text})
                    if len(matches) >= self._settings.max_matches:
                        return {"matches": matches, "truncated": True}
        return {"matches": matches, "truncated": False}

    def _holds(self, path: Path) -> bool:
        """Whether ``path`` resolves inside the checkout (a searched file may be a symlink to anywhere)."""
        try:
            return path.resolve().is_relative_to(self._root)
        except RuntimeError:  # a symlink loop
            return False

    def _run_tests(self, arguments: ToolOutput) -> ToolOutput:
        repository = RunTestsArguments.model_validate(arguments).repo
        if repository not in self._checkout.repositories:
            return {"error": f"unknown repo {repository!r}; choices: {list(self._checkout.repositories)}"}
        timeout = self._settings.run_tests_timeout_seconds
        completed = self._checkout.run_suite(
            repository,
            timeout_seconds=timeout,
            max_output_characters=self._settings.max_output_characters,
        )
        if completed.timed_out:
            summary = f"timed out after {timeout:g} s"
        else:
            summary = extract_last_line(completed.stdout.text) or extract_last_line(completed.stderr.text)
        return {
            "repo": repository,
            "passed": completed.exit_code == 0,
            "returncode": completed.exit_code,
            "summary": summary,
        }

    def _run_benchmark(self, arguments: ToolOutput) -> ToolOutput:
        """The company's OWN inline cost accounting over a sample, as feedback (not the sealed grade)."""
        timeout = self._settings.run_benchmark_timeout_seconds
        completed = self._checkout.run_python(
            ("-c", _BENCHMARK_SOURCE),
            cwd=self._root,
            timeout_seconds=timeout,
            max_output_characters=self._settings.max_output_characters,
        )
        tail = self._settings.error_tail_characters
        if completed.timed_out:
            return {"error": f"benchmark timed out after {timeout:g} s"}
        if completed.exit_code != 0:
            return {"error": "benchmark failed", "stderr": completed.stderr.text[-tail:]}
        try:
            return _JSON_OBJECT.validate_json(extract_last_line(completed.stdout.text))
        except ValidationError:
            return {"error": "benchmark printed no result", "stdout": completed.stdout.text[-tail:]}

    def _bash(self, arguments: ToolOutput) -> ToolOutput:
        """``bash -lc command``: its output, stderr interleaved, kept from the start up to the cap."""
        if not self._shell_enabled:
            return {"error": SHELL_DISABLED_ERROR, "tool": "bash"}
        completed = run_command(
            ("bash", "-lc", BashArguments.model_validate(arguments).command),
            cwd=self._root,
            environment=self._checkout.build_environment(),
            timeout_seconds=self._settings.bash_timeout_seconds,
            max_output_characters=self._settings.max_output_characters,
            kept_end="head",
            merge_stderr=True,
        )
        result = BashResult(
            exit_code=completed.exit_code,
            output=completed.stdout.text,
            truncated=completed.stdout.truncated,
            timed_out=completed.timed_out,
        )
        return result.model_dump(mode="json")


# A small, self-contained agent-facing benchmark: runs a representative datapipe slice over a fixed sample and
# reports the company's inline cost accounting. Directional feedback only; NOT the sealed grade.
_BENCHMARK_SOURCE: Final = r"""
import json
from meridian_common import cost
from meridian_datapipe.dedup import near
from meridian_datapipe.tokenize.tokenizer import Tokenizer
from meridian_datapipe.tokenize.vocab import Vocabulary
from meridian_datapipe.types import Document
cost.reset()
docs = [
    Document(doc_id=f"d{i}", text=("alpha beta gamma delta " * (2 + i % 4)) + f" tail{i % 7}")
    for i in range(40)
]
kept = near.dedup(docs, threshold=0.8)
survivors = [d for d in docs if d.doc_id in set(kept.kept_ids)]
vocab = Vocabulary.from_texts([d.text for d in survivors])
tok = Tokenizer(vocab)
for d in survivors:
    tok.encode(d.text)
print(json.dumps({"cost": cost.total(), "by_kind": cost.snapshot()}))
"""
