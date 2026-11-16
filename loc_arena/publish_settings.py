"""Where ``make publish-run`` stores a graded run, and what its scan refuses: the config's ``publish:``."""

from __future__ import annotations

import re
from re import Pattern

from pydantic import Field, PositiveFloat, PositiveInt, StrictBool, StrictStr

from loc_arena.service_settings import SettingsGroup


class PublishSettings(SettingsGroup):
    """Where ``make publish-run`` stores a run, and what its scan refuses (the ``publish:`` block)."""

    repository: StrictStr | None = Field(
        default=None,
        description="The git URL of the repository that stores published runs; unset, none is published.",
    )
    branch: StrictStr = Field(
        default="main",
        description="The storage repository's branch a run is committed to.",
    )
    readable_files: tuple[StrictStr, ...] = Field(
        default=("scores.json", "config.resolved.yaml", "cost.json", "explorer*.html", "readings"),
        description="The bundle's files and folders, as glob patterns, committed beside the archive, where a "
        "reader opens them.",
    )
    leave_out_rebuilt_checkouts: StrictBool = Field(
        default=True,
        description="The archive leaves out each play's checkout, the graded trees of main with the caches "
        "their grading wrote, when its forge_history rebuilds it byte for byte (rebuild_checkout).",
    )
    part_bytes: PositiveInt = Field(
        default=95_000_000,
        description="The largest file a publish commits; a larger archive is split into numbered parts, as "
        "GitHub refuses a file over 100 MB.",
    )
    key_patterns: tuple[Pattern[str], ...] = Field(
        default=(
            re.compile(r"sk-or-v1-[0-9a-f]{64}"),
            re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
            re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
        ),
        description="The forms of a real key: the provider's and GitHub's. A match anywhere is refused.",
    )
    authorization_pattern: Pattern[str] = Field(
        default=re.compile(
            r"(?i)authorization\\*[\"']?\s*[:=,]\s*\[?\s*b?\\*[\"']?(?:[A-Za-z][\w-]*(?:\s+|%20))?"
            r"(?P<credential>(?=[A-Za-z0-9._~+/=-]*\d)[A-Za-z0-9._~+/=-]{16,})",
        ),
        description="An Authorization header's credential (its group of that name), with a digit, after a "
        "scheme or none, written as a header, a mapping, a pair or URL-encoded, quoted, or escaped as JSON "
        "within JSON; refused unless in-world.",
    )
    in_world_token_pattern: Pattern[str] = Field(
        default=re.compile(r"(?:mi_)?[A-Za-z0-9_-]{43}"),
        description="The form of the company's credentials, each minted for one run as 32 URL-safe random "
        "bytes, which an Authorization header may carry and the run keeps; matched against the whole of one, "
        "less a full stop that ends a sentence.",
    )
    environment_name_pattern: Pattern[str] = Field(
        default=re.compile(
            r"(?i)(?:^|_)(?:key|apikey|token|pat|secret|passwd|pwd|credentials?|auth)$|password$|(?:^|_)(?:secret|password)_",
        ),
        description="A variable of the machine's environment or `.env` whose name matches holds a secret: "
        "its value anywhere in the bundle is refused, as is the password of any URL a variable holds.",
    )
    environment_value_min_length: PositiveInt = Field(
        default=16,
        description="A secret-named variable's value shorter than this is not searched for: it would match "
        "ordinary text.",
    )
    command_seconds: PositiveFloat = Field(
        default=600.0,
        description="The longest one git or gh command of a publish may take; past it the publish fails.",
    )
