"""A small declarative schema validator for Meridian config.

Meridian avoids a heavyweight dependency here: a ``Schema`` is a mapping of key -> ``Field`` describing the
expected type, whether the key is required, a default, and an optional value-level validator. ``validate``
returns a normalized dict or raises ``ValidationError`` with the offending path. It is deliberately small,
deterministic, and dependency-free so every repo can validate its own config block the same way.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from meridian_common.errors import ValidationError

_TYPE_NAMES = {int: "int", float: "float", str: "str", bool: "bool", list: "list", dict: "dict"}


@dataclass(frozen=True)
class Field:
    """One schema field: its expected type, requiredness, default, and an optional validator callable.

    A field may also nest: ``nested`` validates a mapping value against a sub-:class:`Schema`, and ``item``
    validates each element of a list value against a :class:`Field`. These compose so a config block can be as
    deep as needed while still failing loud with the offending path.
    """

    type: type
    required: bool = True
    default: Any = None
    validator: Callable[[Any], bool] | None = None
    help: str = ""
    nested: Schema | None = None
    item: Field | None = None

    def _type_name(self) -> str:
        return _TYPE_NAMES.get(self.type, self.type.__name__)


@dataclass(frozen=True)
class Schema:
    """A mapping of field name -> ``Field``; ``validate`` normalizes a raw mapping against it."""

    fields: dict[str, Field] = field(default_factory=dict)
    allow_extra: bool = False

    def validate(self, raw: dict[str, Any], *, path: str = "") -> dict[str, Any]:
        """Validate ``raw`` against the schema, filling defaults; raise ``ValidationError`` on any problem."""
        if not isinstance(raw, dict):
            raise ValidationError(f"{path or '<root>'} must be a mapping", path=path)
        out: dict[str, Any] = {}
        for name, spec in self.fields.items():
            here = f"{path}.{name}" if path else name
            if name not in raw:
                if spec.required and spec.default is None:
                    raise ValidationError(f"missing required config key {here!r}", path=here)
                out[name] = spec.default
                continue
            value = raw[name]
            out[name] = self._check_value(here, value, spec)
        if not self.allow_extra:
            extra = set(raw) - set(self.fields)
            if extra:
                raise ValidationError(
                    f"unknown config keys under {path or '<root>'}: {sorted(extra)}",
                    path=path,
                )
        return out

    @staticmethod
    def _check_value(here: str, value: Any, spec: Field) -> Any:
        # bool is a subclass of int; keep them distinct so a flag is never silently an int and vice versa
        if spec.type is int and isinstance(value, bool):
            raise ValidationError(f"{here} must be an int, got bool", path=here)
        if spec.type is bool and not isinstance(value, bool):
            raise ValidationError(f"{here} must be a bool", path=here)
        if not isinstance(value, spec.type):
            raise ValidationError(
                f"{here} must be {spec._type_name()}, got {type(value).__name__}",
                path=here,
            )
        if spec.nested is not None and isinstance(value, dict):
            value = spec.nested.validate(value, path=here)
        if spec.item is not None and isinstance(value, list):
            value = [Schema._check_value(f"{here}[{i}]", v, spec.item) for i, v in enumerate(value)]
        if spec.validator is not None and not spec.validator(value):
            raise ValidationError(f"{here} failed validation", path=here)
        return value
