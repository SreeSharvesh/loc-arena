"""P14 CP4: the curated public tree excludes prompts and internal docs; it keeps the code and the site.

Builds the public tree via the allowlist and runs the denylist verifier: the exclusion guarantee is part of
the gate, so a leak of a prompt, an internal doc, CLAUDE.md, a diagram, or a secret fails the build.
"""

from __future__ import annotations

from pathlib import Path

from scripts.make_public import build_public_tree, verify_public_tree

_REPO = Path(__file__).resolve().parents[2]


def test_public_tree_excludes_internals_and_keeps_the_essentials(tmp_path: Path) -> None:
    dest = tmp_path / "public-repo"
    copied = build_public_tree(_REPO, dest)
    assert copied  # something was copied

    # the exclusion verifier is clean (no prompt, internal doc, CLAUDE.md, diagram, or secret)
    assert verify_public_tree(dest) == []

    # every excluded top-level path is absent
    for excluded in ("prompts", "docs", "CLAUDE.md", "diagrams", ".env"):
        assert not (dest / excluded).exists(), f"{excluded} leaked into the public tree"

    # the public tree keeps the code, configs, scenarios, tests, the committed golden example, and the README
    for included in (
        "loc_arena",
        "company",
        "configs",
        "scenarios",
        "tests",
        "compose.yaml",
        "Makefile",
        "README.md",
        ".env.example",
    ):
        assert (dest / included).exists(), f"{included} missing from the public tree"

    # no phase-progress or internal spec file slipped in by any path
    for bad in dest.rglob("*"):
        name = bad.name
        assert "_PROGRESS" not in name
        assert name not in {
            "DECISIONS.md",
            "SETTING_SPEC.md",
            "COMPANY_CODEBASE_BLUEPRINT.md",
            "ENVIRONMENT_AND_TASKS.md",
            "AUDITING_AND_RUNNING.md",
            "IMPLEMENTATION_PLAN.md",
            "RUN_01.md",
        }


def test_verifier_flags_a_planted_leak(tmp_path: Path) -> None:
    dest = tmp_path / "public-repo"
    build_public_tree(_REPO, dest)
    assert verify_public_tree(dest) == []
    # plant an excluded doc and a fake key; the verifier must catch both. The fake key is BUILT at runtime
    # from parts so this test source does not itself contain the sk-or pattern (which would trip the verifier
    # on the copied public tree).
    fake_key = "sk-or-" + "abcdefgh12345678"
    (dest / "prompts").mkdir(exist_ok=True)
    (dest / "prompts" / "P0.md").write_text("secret prompt")
    (dest / "configs" / "leak.yaml").write_text(f"key: {fake_key}\n")
    violations = verify_public_tree(dest)
    assert any("prompts/P0.md" in v for v in violations)
    assert any("provider key" in v for v in violations)
