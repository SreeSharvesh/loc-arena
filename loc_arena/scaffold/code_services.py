"""Real code/test/benchmark tools over the agent's company checkout.

Owns the ``code`` tool catalog (read/write/edit/list/grep, ``run_tests``, ``run_benchmark``, and ``bash`` when
sandboxes are given) so the untrusted
team actually works the seven-repo ``company/`` checkout: it reads and edits the real code, runs each repo's
suite, and profiles cost, then that same checkout is graded (continuous scoring). Composes as a
``Services`` layer
(``run(tool, args) -> dict``) beneath the covert/forge layers; a tool it does not own falls through.

Enforces the sealed-vs-tamperable isolation at the workspace boundary: every path is confined to the
checkout, so the untrusted agent
(even as root in its own sandbox) has no route out to the sealed layer. The tools that run agent-written
code (``bash``, ``run_tests``, ``run_benchmark``) run in the calling agent's own sandbox when sandboxes are
given (a stack run), which sees the checkouts and nothing of the episode; an agent with none gets an error and
nothing runs. Without sandboxes, ``bash`` is refused, since it is not confined to the checkout, and the other
two run in this process. ``run_tests`` and ``run_benchmark`` run the scenario's skill scripts, which a stack
run's agents run with bash instead, with the plain venv interpreter (never ``uv run``, which cannot resolve
the copied repos' deps). The benchmark reports the company's own inline cost accounting as honest DIRECTIONAL
feedback; it never reads or moves the sealed grade meter.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from sandbox_server.command import run_command
from sandbox_server.wire import CommandRequest, CommandResult

from loc_arena.gateway.core import environment_without_key
from loc_arena.sandbox import SandboxClient, SandboxError
from loc_arena.scaffold.tools import Services
from loc_arena.settings import StackSettings

_MAX_READ = 200_000  # cap a read so a huge file can't blow the context
_MAX_MATCHES = 200


class CodeServices:
    """A ``Services`` layer whose code/test/bench tools act on one company checkout; others fall through."""

    _OWNED = frozenset(
        {
            "read_file",
            "write_file",
            "edit_file",
            "list_dir",
            "search_code",
            "grep",
            "run_tests",
            "run_benchmark",
            "bash",
        },
    )

    def __init__(
        self,
        base: Services,
        *,
        checkout: Path,
        repos: list[str],
        stack: StackSettings,
        sandboxes: Mapping[str, SandboxClient] | None = None,
        skills: Path | None = None,
        python_exe: str = sys.executable,
    ) -> None:
        """Wire the layer to a base ``Services``, the checkout, the repos, the settings and the interpreter.

        ``sandboxes``, each agent's by its id, are given only in the episode container: an agent's code then
        runs in its own, ``bash`` included. Without them, ``bash`` is refused. ``skills`` is the scenario's
        skills folder where the agents' code runs.
        """
        self._base = base
        self._checkout = checkout.resolve()
        self._repos = list(repos)
        self._stack = stack
        self._sandboxes = sandboxes
        self._skills = skills
        self._py = python_exe

    def _resolve(self, rel: str) -> Path:
        """Resolve ``rel`` under the checkout, refusing any path that escapes it (workspace confinement)."""
        p = (self._checkout / rel).resolve()
        if p != self._checkout and self._checkout not in p.parents:
            raise ValueError(f"path escapes the checkout: {rel}")
        return p

    def _pythonpath(self) -> str:
        """The ``PYTHONPATH`` that makes the seven repos importable: each repo dir under the checkout."""
        return os.pathsep.join(str(self._checkout / r) for r in self._repos)

    def _execute(
        self,
        agent: str,
        argv: Sequence[str],
        directory: Path,
        timeout_seconds: float,
    ) -> CommandResult:
        """Run agent-written code, never with the key: in ``agent``'s own sandbox, else here.

        With sandboxes, an agent with none of its own runs nothing anywhere.
        """
        request = CommandRequest(
            argv=list(argv),
            directory=directory,
            timeout_seconds=timeout_seconds,
            environment={"PYTHONPATH": self._pythonpath()},
        )
        if self._sandboxes is None:
            return run_command(request, self._stack.command_output_limit_bytes, environment_without_key())
        if agent not in self._sandboxes:
            raise SandboxError(f"{agent} has no sandbox, so its command did not run")
        return self._sandboxes[agent].run(request)

    def run(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch an owned code tool; a tool this layer does not own falls through to the base."""
        if tool not in self._OWNED:
            return self._base.run(tool, args)
        try:
            handler = getattr(self, f"_t_{tool}")
            return cast(dict[str, Any], handler(args))
        except (ValueError, KeyError, TypeError) as exc:
            # a live model routinely omits/mistypes an arg -> a logged error result, never a crash
            return {"error": f"bad args for {tool}: {exc}", "tool": tool}
        except SandboxError as exc:  # agent code can kill its own sandbox: the episode plays on
            return {"error": str(exc), "tool": tool}

    # --- code tools ----------------------------------------------------------------------------------
    def _t_read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        p = self._resolve(str(args.get("path", "")))
        if not p.is_file():
            return {"error": "not a file", "path": str(args.get("path", ""))}
        text = p.read_text(errors="replace")
        return {"path": args["path"], "content": text[:_MAX_READ], "truncated": len(text) > _MAX_READ}

    def _t_write_file(self, args: dict[str, Any]) -> dict[str, Any]:
        p = self._resolve(str(args["path"]))
        p.parent.mkdir(parents=True, exist_ok=True)
        content = str(args.get("content", ""))
        p.write_text(content)
        return {"path": args["path"], "bytes_written": len(content.encode())}

    def _t_edit_file(self, args: dict[str, Any]) -> dict[str, Any]:
        p = self._resolve(str(args["path"]))
        if not p.is_file():
            return {"error": "not a file", "path": args["path"]}
        old, new = str(args["old"]), str(args.get("new", ""))
        text = p.read_text()
        count = text.count(old)
        if count == 0:
            return {"error": "old string not found", "path": args["path"], "replaced": 0}
        p.write_text(text.replace(old, new))
        return {"path": args["path"], "replaced": count}

    def _t_list_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        p = self._resolve(str(args.get("path", "")))
        if not p.is_dir():
            return {"error": "not a directory", "path": args.get("path", "")}
        entries = [
            {"name": c.name, "type": "dir" if c.is_dir() else "file"}
            for c in sorted(p.iterdir())
            if c.name not in ("__pycache__", ".pytest_cache", ".git")
        ]
        return {"path": args.get("path", ""), "entries": entries}

    def _t_search_code(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = str(args["pattern"])
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return {"error": f"bad regex: {exc}"}
        root = self._resolve(str(args.get("path", "")))
        matches: list[dict[str, Any]] = []
        for f in sorted(root.rglob("*.py")):
            if any(part in ("__pycache__", ".pytest_cache", ".git") for part in f.parts):
                continue
            try:
                for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                    if rx.search(line):
                        rel = str(f.relative_to(self._checkout))
                        matches.append({"file": rel, "line": i, "text": line.strip()[:200]})
                        if len(matches) >= _MAX_MATCHES:
                            return {"matches": matches, "truncated": True}
            except OSError:
                continue
        return {"matches": matches, "truncated": False}

    _t_grep = _t_search_code

    # --- the skills' scripts as tools, offered in process alone; #99 step 6 removes them --------------
    def _t_run_tests(self, args: dict[str, Any]) -> dict[str, Any]:
        repo = str(args.get("repo", ""))
        return self._run_skill(
            args,
            "run-tests/scripts/run_tests.py",
            [repo],
            self._stack.run_tests_timeout_seconds,
        )

    def _t_run_benchmark(self, args: dict[str, Any]) -> dict[str, Any]:
        """Agent-facing cost feedback via the company's OWN inline meter (not the sealed grade)."""
        timeout = self._stack.run_benchmark_timeout_seconds
        return self._run_skill(args, "run-benchmark/scripts/run_benchmark.py", [], timeout)

    def _run_skill(
        self,
        args: dict[str, Any],
        script: str,
        arguments: list[str],
        timeout: float,
    ) -> dict[str, Any]:
        """What the skill ``script`` prints last, run with ``arguments`` from the checkout."""
        if self._skills is None:
            return {"error": "this scenario has no skills"}
        command = [self._py, str(self._skills / script), *arguments]
        res = self._execute(str(args["actor_uid"]), command, self._checkout, timeout)
        try:
            return cast(dict[str, Any], json.loads(res.stdout.strip().splitlines()[-1]))
        except (IndexError, json.JSONDecodeError):
            return {"error": f"{script} printed no result", "stderr": res.stderr[-500:]}

    # --- shell ---------------------------------------------------------------------------------------
    def _t_bash(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run ``bash -c <command>`` in the checkout, in the agent's sandbox; a timeout kills its session.

        Not a login shell: Debian's ``/etc/profile`` would reset ``PATH`` and drop the image's virtualenv.
        """
        if self._sandboxes is None:
            return {"error": "bash runs only in a stack run's sandbox", "tool": "bash"}
        timeout = self._stack.shell_timeout_seconds
        command = ["bash", "-c", str(args["command"])]
        res = self._execute(str(args["actor_uid"]), command, self._checkout, timeout)
        if res.returncode is None:
            return {**res.model_dump(), "error": f"timed out after {timeout:g} seconds"}
        return res.model_dump()
