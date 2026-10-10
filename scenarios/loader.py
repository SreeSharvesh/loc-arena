"""Scenario-pack loader.

Enforces config over code: a run config's ``scenario: <name>`` resolves to a directory
``scenarios/<name>/`` holding ``scenario.yaml`` (the scorer/verifier names and seed repo), ``main.py``
(imports register its scorer), optional ``side.py`` (imports register its verifier), a ``seed/`` repo
overlay, the scripted policy's ``scripted/`` moves and a sealed ``reference/``. Loading a scenario imports
its ``main.py``/``side.py`` so the registrations run, then exposes the seed, the moves and the sealed
reference to the harness. Adding a (main, side) pair at a new point is: drop a pack and register its
scorer/verifier -- no engine change.

A pack's ``services:`` declares the services its world has. An entry with ``build`` or ``image`` is live: a
stack run gives it its own container on agent-net, reached at ``http://<name>:<port>``. Any other entry stays
simulated, and the stack ignores it.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Self

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from sandbox_server.confinement import resolve_inside

SCENARIOS_ROOT = Path(__file__).resolve().parent
# A host name label (RFC 1123) in lower case alone: what names a compose service and its host on agent-net.
DNS_LABEL: Final = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
# The stack's own services (loc_arena.episode_stack): a live service may not take their names.
STACK_SERVICES: Final = frozenset({"gateway", "episode"})
SANDBOX_PREFIX: Final = "sandbox-"
# Never in a service's build context: the sealed answer and the scripted moves, which agents must not reach.
SEALED_DIRECTORIES: Final = ("reference", "scripted")
PACK_DIRECTORY: Final = "pack_directory"  # the validation context key: the pack a build path resolves in
CredentialName = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z0-9-]+$")]


def _require_distinct(names: tuple[str, ...]) -> tuple[str, ...]:
    """Refuse a credential named twice: compose will not mount one secret twice in a container."""
    if len(set(names)) < len(names):
        raise ValueError(f"it names a credential twice: {list(names)}")
    return names


CredentialNames = Annotated[tuple[CredentialName, ...], AfterValidator(_require_distinct)]


def _require_a_service_name(name: str) -> str:
    """Refuse a name that is no host name, or that would take the place of one of the stack's services."""
    if not DNS_LABEL.fullmatch(name):
        raise ValueError("it must be a DNS label: at most 63 lower-case letters, digits and inner hyphens")
    if name in STACK_SERVICES or name.startswith(SANDBOX_PREFIX):
        raise ValueError(f"it is taken by the stack: {sorted(STACK_SERVICES)} and {SANDBOX_PREFIX}<agent id>")
    return name


class ScenarioService(BaseModel):
    """One entry of a pack's ``services:``, as written; ``build`` or ``image`` makes it live."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[StrictStr, AfterValidator(_require_a_service_name)] = Field(
        description="The compose service and its host name on agent-net: the entry's key.",
    )
    build: Path | None = Field(
        default=None,
        description="A directory under the pack holding a Dockerfile; resolved to its absolute path.",
    )
    image: StrictStr | None = Field(default=None, min_length=1, description="A ready image to run instead.")
    command: tuple[StrictStr, ...] | None = Field(
        default=None,
        min_length=1,
        description="What the container runs; the image's own command when absent.",
    )
    port: StrictInt | None = Field(default=None, ge=1, le=65535, description="Where the service listens.")
    healthcheck: tuple[StrictStr, ...] | None = Field(
        default=None,
        min_length=1,
        description="The compose healthcheck test; a probe of the port when absent.",
    )
    accepts: CredentialNames = Field(
        default=(),
        description="The credentials the service checks; none: open to everything on agent-net.",
    )
    description: StrictStr = Field(default="", description="What the agents of a stack run are told it is.")

    @field_validator("build")
    @classmethod
    def _resolve_build(cls, build: Path | None, info: ValidationInfo) -> Path | None:
        """The absolute build directory: under the pack, outside its sealed parts, with a Dockerfile."""
        if build is None:
            return None
        if info.context is None:
            raise ValueError("a build directory resolves only against its pack, given as the context")
        pack = info.context[PACK_DIRECTORY].resolve()
        resolved = resolve_inside(pack, build)
        if resolved == pack or any(resolved.is_relative_to(pack / sealed) for sealed in SEALED_DIRECTORIES):
            raise ValueError(
                f"{build} must be a directory under the pack, outside {list(SEALED_DIRECTORIES)}",
            )
        if not (resolved / "Dockerfile").is_file():
            raise ValueError(f"{build} holds no Dockerfile")
        return resolved

    @model_validator(mode="after")
    def _require_one_source_and_a_port_when_live(self) -> Self:
        """Refuse both build and image, a live entry with no port, or a ready image with no healthcheck.

        The default probe runs python, which a ready image need not hold; a build is ours to give it.
        """
        if self.build is not None and self.image is not None:
            raise ValueError("give build or image, not both")
        if (self.build is not None or self.image is not None) and self.port is None:
            raise ValueError("a live service (build or image) needs a port")
        if self.image is not None and self.healthcheck is None:
            raise ValueError("a ready image needs a healthcheck: the default probe runs python")
        return self


@dataclass(frozen=True)
class LiveService:
    """A service a stack run gives its own container on agent-net."""

    name: str
    source: Path | str  # the absolute build directory, or the ready image
    port: int
    command: tuple[str, ...] | None
    healthcheck: tuple[str, ...] | None
    accepts: tuple[str, ...]
    description: str


def accepted_credentials(services: tuple[LiveService, ...]) -> tuple[str, ...]:
    """Every credential some of ``services`` checks, each once, in declaration order."""
    return tuple(dict.fromkeys(credential for service in services for credential in service.accepts))


@dataclass(frozen=True)
class Scenario:
    """A loaded scenario pack: its directory, seed and reference paths, and its scorer/verifier names."""

    name: str
    directory: Path
    scorer: str
    verifier: str
    seed_repo: str
    meta: dict[str, Any]
    live_services: tuple[LiveService, ...] = ()

    @property
    def seed_dir(self) -> Path:
        """The pack's ``seed/`` repo overlay directory."""
        return self.directory / "seed"

    @property
    def reference_dir(self) -> Path:
        """The pack's sealed ``reference/`` directory (read only by the grader)."""
        return self.directory / "reference"

    @property
    def scripted_dir(self) -> Path:
        """The pack's ``scripted/`` directory: the scripted policy's moves, mounted into scripted episodes."""
        return self.directory / "scripted"

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


def _parse_live_services(directory: Path, declared: object) -> tuple[LiveService, ...]:
    """The live entries of ``declared``, a pack's ``services:``; a ValueError names an invalid entry."""
    if not isinstance(declared, dict):
        raise ValueError(f"scenario {directory.name}: services must map each name to its entry")
    live = []
    for name, entry in declared.items():
        try:
            service = ScenarioService.model_validate(
                {**(entry or {}), "name": name},
                context={PACK_DIRECTORY: directory},
            )
        except (ValidationError, TypeError) as error:
            raise ValueError(f"scenario {directory.name}: service {name!r} is invalid: {error}") from error
        source = service.build or service.image
        if source is not None and service.port is not None:  # a live entry with no port was refused above
            live.append(
                LiveService(
                    name=service.name,
                    source=source,
                    port=service.port,
                    command=service.command,
                    healthcheck=service.healthcheck,
                    accepts=service.accepts,
                    description=service.description,
                ),
            )
    return tuple(live)


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
        live_services=_parse_live_services(directory, meta.get("services") or {}),
    )
