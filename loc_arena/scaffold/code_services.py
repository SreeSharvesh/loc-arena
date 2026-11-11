"""The native tools over each agent's own checkout: read, write, edit, list and search, and ``bash``.

Each agent works its own clone of the company's seven repos (``loc_arena.checkouts``); what it merges on the
forge's ``main`` is graded. Every path is confined to the calling agent's checkout, so the untrusted agent
(even as root in its own sandbox) has no route out to the sealed layer. ``bash`` runs agent-written code where
that agent's commands run: in its own sandbox in a stack run, which sees its checkout and nothing of the
episode; in this process otherwise, where only the deterministic fixture's scripted moves play. An agent with
no checkout gets an error and nothing runs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple, cast

from sandbox_server.wire import CommandRequest, CommandResult

from loc_arena.checkouts import Checkout
from loc_arena.sandbox import SandboxError
from loc_arena.settings import StackSettings

_MAX_READ = 200_000  # cap a read so a huge file can't blow the context
_MAX_MATCHES = 200


class SkillsFolder(NamedTuple):
    """Where the sandboxes mount the skills (``mounted``), and the folder holding them here (``source``)."""

    mounted: Path
    source: Path


class CodeServices:
    """The native tools, each acting on the calling agent's checkout."""

    TOOLS = frozenset({"read_file", "write_file", "edit_file", "list_dir", "search_code", "bash"})

    def __init__(
        self,
        *,
        checkouts: Mapping[str, Checkout],
        repos: list[str],
        stack: StackSettings,
        skills: SkillsFolder | None = None,
    ) -> None:
        """Act on each agent's checkout of ``checkouts``, whose ``repos`` are importable, under ``stack``.

        ``read_file`` and ``list_dir`` also read the ``skills`` folder.
        """
        self._checkouts = dict(checkouts)
        self._repos = list(repos)
        self._stack = stack
        self._skills = skills._replace(source=skills.source.resolve()) if skills else None

    def _checkout(self, args: dict[str, Any]) -> Checkout:
        agent = str(args["actor_uid"])
        if agent not in self._checkouts:
            raise SandboxError(f"{agent} has no checkout, so nothing ran")
        return self._checkouts[agent]

    def _resolve(self, args: dict[str, Any], *, required: bool = False) -> tuple[Path, Path]:
        """The caller's checkout, and ``args["path"]`` under it, refusing any path that escapes it.

        Without a path, the checkout itself, unless one is ``required``.
        """
        root = self._checkout(args).directory
        rel = str(args["path"] if required else args.get("path", ""))
        p = (root / rel).resolve()
        if p != root and root not in p.parents:
            raise ValueError(f"path escapes the checkout: {rel}")
        return root, p

    def _resolve_read(self, args: dict[str, Any]) -> Path:
        """``args["path"]`` to read: inside the skills folder when it names one, else under the checkout."""
        rel = Path(str(args.get("path", "")))
        if self._skills and rel.is_relative_to(self._skills.mounted):
            p = (self._skills.source / rel.relative_to(self._skills.mounted)).resolve()
            if p == self._skills.source or self._skills.source in p.parents:
                return p
        return self._resolve(args)[1]

    def run(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run the native tool ``tool`` with ``args``; a malformed call is an error result, never a crash."""
        try:
            return cast(dict[str, Any], getattr(self, f"_t_{tool}")(args))
        except (ValueError, KeyError, TypeError) as exc:
            # a live model routinely omits/mistypes an arg -> a logged error result, never a crash
            return {"error": f"bad args for {tool}: {exc}", "tool": tool}
        except SandboxError as exc:  # agent code can kill its own sandbox: the episode plays on
            return {"error": str(exc), "tool": tool}
        # a path the filesystem refuses, as a name too long: an error result, with no host path in it
        except OSError as exc:
            return {"error": f"{tool} failed: {exc.strerror}", "tool": tool}

    # --- code tools ----------------------------------------------------------------------------------
    def _t_read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        p = self._resolve_read(args)
        if not p.is_file():
            return {"error": "not a file", "path": str(args.get("path", ""))}
        text = p.read_text(errors="replace")
        return {"path": args["path"], "content": text[:_MAX_READ], "truncated": len(text) > _MAX_READ}

    def _t_write_file(self, args: dict[str, Any]) -> dict[str, Any]:
        _, p = self._resolve(args, required=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        content = str(args.get("content", ""))
        p.write_text(content)
        return {"path": args["path"], "bytes_written": len(content.encode())}

    def _t_edit_file(self, args: dict[str, Any]) -> dict[str, Any]:
        _, p = self._resolve(args, required=True)
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
        p = self._resolve_read(args)
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
        checkout, root = self._resolve(args)
        matches: list[dict[str, Any]] = []
        for f in sorted(root.rglob("*.py")):
            if any(part in ("__pycache__", ".pytest_cache", ".git") for part in f.parts):
                continue
            try:
                for i, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                    if rx.search(line):
                        rel = str(f.relative_to(checkout))
                        matches.append({"file": rel, "line": i, "text": line.strip()[:200]})
                        if len(matches) >= _MAX_MATCHES:
                            return {"matches": matches, "truncated": True}
            except OSError:
                continue
        return {"matches": matches, "truncated": False}

    # --- shell ---------------------------------------------------------------------------------------
    def _t_bash(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run ``bash -c <command>`` in the caller's checkout, where its commands run; a timeout kills all."""
        checkout, timeout = self._checkout(args), self._stack.shell_timeout_seconds
        res = self._run(checkout, str(args["command"]), checkout.directory, timeout)
        if res.returncode is None:
            return {**res.model_dump(), "error": f"timed out after {timeout:g} seconds"}
        return res.model_dump()

    def _run(self, checkout: Checkout, command: str, directory: Path, timeout: float) -> CommandResult:
        """Run ``command`` from ``directory`` where ``checkout``'s commands run.

        Not a login shell: Debian's ``/etc/profile`` would reset ``PATH`` and drop the image's virtualenv.
        """
        request = CommandRequest(
            argv=["bash", "-c", command],
            directory=directory,
            timeout_seconds=timeout,
            # The seven repos importable: each repo's directory in the checkout.
            environment={"PYTHONPATH": os.pathsep.join(str(checkout.directory / r) for r in self._repos)},
        )
        return checkout.runner.run(request)
