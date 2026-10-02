"""The code tools as a ``Services`` layer over a local checkout; a tool it does not own falls through."""

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
    ) -> None:
        """Wire the layer to a base ``Services``, the checkout, the repo names, and the interpreter."""
        self._base = base
        self._workspace = Workspace(Checkout(checkout, tuple(repos), python_exe), ExecutionSettings())

    def run(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Run an owned code tool in the workspace; a tool this layer does not own falls through."""
        if tool not in CODE_TOOL_NAMES:
            return self._base.run(tool, args)
        call = CodeToolCall.model_validate({"tool": tool, "arguments": args})
        return dict(self._workspace.run(call).result)
