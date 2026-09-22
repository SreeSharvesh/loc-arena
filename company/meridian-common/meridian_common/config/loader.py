"""The layered Meridian config loader: defaults, then YAML files, then environment overrides.

Precedence is lowest to highest: hard defaults < each YAML layer in order < environment variables. A YAML
layer
deep-merges onto the accumulated config (nested mappings merge; scalars and lists replace). Environment
overrides are read from ``MERIDIAN_<SECTION>__<KEY>`` (double underscore separates nesting) and coerced to the
type of the value they replace. The result may be validated against a
:class:`~meridian_common.config.schema.Schema`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from meridian_common.config.schema import Schema
from meridian_common.errors import ConfigError

_ENV_PREFIX = "MERIDIAN_"


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``override`` onto ``base`` (nested mappings merge; other values replace). Pure."""
    out = dict(base)
    for key, value in override.items():
        existing = out.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            out[key] = deep_merge(existing, value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}", code="config.missing", path=str(path))
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must be a mapping at top level", path=str(path))
    return data


def _coerce(raw: str, like: Any) -> Any:
    """Coerce an env string to the type of the value it overrides (bool/int/float/str)."""
    if isinstance(like, bool):
        low = raw.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"cannot parse {raw!r} as bool", code="config.coerce")
    if isinstance(like, int):
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"cannot parse {raw!r} as int", code="config.coerce") from exc
    if isinstance(like, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"cannot parse {raw!r} as float", code="config.coerce") from exc
    return raw


def _apply_env(config: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    """Overlay ``MERIDIAN_A__B=...`` variables onto ``config`` (double underscore separates nesting)."""
    out = dict(config)
    for name, value in environ.items():
        if not name.startswith(_ENV_PREFIX):
            continue
        parts = name[len(_ENV_PREFIX) :].lower().split("__")
        cursor = out
        for part in parts[:-1]:
            nxt = cursor.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cursor[part] = nxt
            cursor = nxt
        leaf = parts[-1]
        like = cursor.get(leaf, "")
        cursor[leaf] = _coerce(value, like)
    return out


def load_config(
    *,
    defaults: dict[str, Any] | None = None,
    files: list[Path] | None = None,
    environ: dict[str, str] | None = None,
    schema: Schema | None = None,
) -> dict[str, Any]:
    """Compose config from defaults, YAML layers, and env overrides; optionally validate against ``schema``.

    ``environ`` defaults to the process environment. When a ``schema`` is given the composed mapping is
    validated (defaults filled, unknown keys rejected unless the schema allows extras) and the normalized
    mapping is returned.
    """
    config: dict[str, Any] = dict(defaults or {})
    for path in files or []:
        config = deep_merge(config, _load_yaml(path))
    config = _apply_env(config, dict(os.environ) if environ is None else environ)
    if schema is not None:
        return schema.validate(config)
    return config
