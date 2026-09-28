"""Real code, test and benchmark tools over the agents' company checkout, run on this machine.

A ``Services`` layer (``run(tool, args) -> dict``) beneath the covert and forge layers: it owns the code
tools (``CodeToolName``: read, write, edit, list and search files, ``run_tests``, ``run_benchmark``,
``profile`` and ``bash``) and runs them in a local :class:`~loc_arena.execution.workspace.Workspace`
over the checkout that is then graded; a tool it does not own falls through to the base layer. The
workspace confines every path to the checkout. Here (STACK=0) agent code runs on this machine with its
credentials in reach, so ``bash`` answers with an error unless a caller passes ``shell_enabled=True``; in
the stack the same tools, the shell included, run in each agent's own sandbox (``ExecutionClient``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final, get_args

from loc_arena.execution.checkout import Checkout
from loc_arena.execution.workspace import Workspace
from loc_arena.scaffold.tools import Services, ToolResult
from loc_arena.stack.contracts import CodeToolCall, CodeToolName
from loc_arena.stack.settings import ExecutionSettings

CODE_TOOL_NAMES: Final = frozenset(get_args(CodeToolName))


class CodeServices:
    """A ``Services`` layer whose code tools act on one company checkout; others fall through."""

    def __init__(
        self,
        base: Services,
        *,
        checkout: Path,
        repos: list[str],
        python_exe: str = sys.executable,
        settings: ExecutionSettings | None = None,
        shell_enabled: bool = False,
    ) -> None:
        """Wire the layer to a base ``Services``, the checkout, its repos, the interpreter and the limits.

        ``settings`` defaults to the model defaults; pass ``config.settings.execution``. ``shell_enabled``
        stays off on the host: ``bash`` then gives an error result and starts no process.
        """
        self._base = base
        self._workspace = Workspace(
            Checkout(checkout, tuple(repos), python_exe),
            settings if settings is not None else ExecutionSettings(),
            shell_enabled=shell_enabled,
        )

    def run(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Run an owned code tool in the workspace; a tool this layer does not own falls through."""
        if tool not in CODE_TOOL_NAMES:
            return self._base.run(tool, args)
        call = CodeToolCall.model_validate({"tool": tool, "arguments": args})
        return dict(self._workspace.run(call).result)
