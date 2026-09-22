"""Scenario-pack loader.

Enforces config over code: a run config's ``scenario: <name>`` resolves to a directory
``scenarios/<name>/`` holding ``scenario.yaml`` (the scorer/verifier names and seed repo), ``main.py``
(imports register its scorer), optional ``side.py`` (imports register its verifier), a ``seed/`` repo
overlay, and a sealed ``reference/``. Loading a scenario imports its ``main.py``/``side.py`` so the
registrations run, then exposes the seed and sealed reference to the harness. Adding a (main, side) pair at
a new point is: drop a pack and register its scorer/verifier -- no engine change.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SCENARIOS_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Scenario:
    """A loaded scenario pack: its directory, seed and reference paths, and its scorer/verifier names."""

    name: str
    directory: Path
    scorer: str
    verifier: str
    seed_repo: str
    meta: dict[str, Any]

    @property
    def seed_dir(self) -> Path:
        """The pack's ``seed/`` repo overlay directory."""
        return self.directory / "seed"

    @property
    def reference_dir(self) -> Path:
        """The pack's sealed ``reference/`` directory (read only by the grader)."""
        return self.directory / "reference"

    def repo_seed(self) -> Path:
        """The seeded repo the agent works on (``seed/<seed_repo>``)."""
        return self.seed_dir / self.seed_repo


def _import_pack_module(directory: Path, module: str) -> None:
    path = directory / f"{module}.py"
    if not path.exists():
        return
    mod_name = f"scenarios.{directory.name}.{module}"
    if mod_name in sys.modules:
        return  # already imported: re-running exec_module would re-register a NEW callable and trip the guard
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod  # register BEFORE exec so a second load short-circuits (idempotent)
    try:
        spec.loader.exec_module(mod)  # side effect: register_scorer / register_verifier run
    except Exception:
        sys.modules.pop(mod_name, None)  # a failed import must not leave a half-registered module cached
        raise


def load_scenario(name: str, *, root: Path | None = None) -> Scenario:
    """Load a scenario pack by name: run its registrations and return its ``Scenario`` handle."""
    directory = (root or SCENARIOS_ROOT) / name
    meta_path = directory / "scenario.yaml"
    if not meta_path.exists():
        raise FileNotFoundError(f"no scenario pack at {directory} (missing scenario.yaml)")
    meta: dict[str, Any] = yaml.safe_load(meta_path.read_text())
    _import_pack_module(directory, "main")
    _import_pack_module(directory, "side")
    return Scenario(
        name=str(meta.get("name", name)),
        directory=directory,
        scorer=str(meta["scorer"]),
        verifier=str(meta["verifier"]),
        seed_repo=str(meta.get("seed_repo", "meridian-serving")),
        meta=meta,
    )
