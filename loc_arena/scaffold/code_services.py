"""The native tools over the agent's company checkout: read, write, edit, list and search, and ``bash``.

The untrusted team works the real seven-repo ``company/`` checkout with them, and that same checkout is graded
(continuous scoring). Every path is confined to the checkout, so the untrusted agent (even as root in its own
sandbox) has no route out to the sealed layer. ``bash`` runs agent-written code, so it runs only in the
calling agent's own sandbox, given in a stack run, which sees the checkouts and nothing of the episode;
without sandboxes it is refused, since it is not confined to the checkout. An agent with no sandbox of its
own gets an error and nothing runs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from sandbox_server.wire import CommandRequest

from loc_arena.sandbox import SandboxClient, SandboxError
from loc_arena.settings import StackSettings

_MAX_READ = 200_000  # cap a read so a huge file can't blow the context
_MAX_MATCHES = 200


class CodeServices:
    """The native tools, acting on one company checkout."""

    TOOLS = frozenset({"read_file", "write_file", "edit_file", "list_dir", "search_code", "bash"})

    def __init__(
        self,
        *,
        checkout: Path,
        repos: list[str],
        stack: StackSettings,
        sandboxes: Mapping[str, SandboxClient] | None = None,
    ) -> None:
        """Act on ``checkout``, whose ``repos`` are importable, under the ``stack`` settings.

        ``sandboxes``, each agent's by its id, are given only in the episode container: an agent's code then
        runs in its own, ``bash`` included. Without them, ``bash`` is refused.
        """
        self._checkout = checkout.resolve()
        self._repos = list(repos)
        self._stack = stack
        self._sandboxes = sandboxes

    def _resolve(self, rel: str) -> Path:
        """Resolve ``rel`` under the checkout, refusing any path that escapes it (workspace confinement)."""
        p = (self._checkout / rel).resolve()
        if p != self._checkout and self._checkout not in p.parents:
            raise ValueError(f"path escapes the checkout: {rel}")
        return p

    def _pythonpath(self) -> str:
        """The ``PYTHONPATH`` that makes the seven repos importable: each repo dir under the checkout."""
        return os.pathsep.join(str(self._checkout / r) for r in self._repos)

    def run(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run the native tool ``tool`` with ``args``; a malformed call is an error result, never a crash."""
        try:
            return cast(dict[str, Any], getattr(self, f"_t_{tool}")(args))
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

    # --- shell ---------------------------------------------------------------------------------------
    def _t_bash(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run ``bash -c <command>`` in the checkout, in the agent's sandbox; a timeout kills its session.

        Not a login shell: Debian's ``/etc/profile`` would reset ``PATH`` and drop the image's virtualenv.
        """
        if self._sandboxes is None:
            return {"error": "bash runs only in a stack run's sandbox", "tool": "bash"}
        agent = str(args["actor_uid"])
        if agent not in self._sandboxes:
            raise SandboxError(f"{agent} has no sandbox, so its command did not run")
        timeout = self._stack.shell_timeout_seconds
        request = CommandRequest(
            argv=["bash", "-c", str(args["command"])],
            directory=self._checkout,
            timeout_seconds=timeout,
            environment={"PYTHONPATH": self._pythonpath()},
        )
        res = self._sandboxes[agent].run(request)
        if res.returncode is None:
            return {**res.model_dump(), "error": f"timed out after {timeout:g} seconds"}
        return res.model_dump()
