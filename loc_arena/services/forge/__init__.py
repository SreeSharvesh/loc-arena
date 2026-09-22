"""Forge: git server plus minimal CI (tamperable).

Enforces the sealed-vs-tamperable isolation; PR-review control point.

Owns the hosted company repos and their branches, PR creation, and a minimal CI. Control-plane repos require
reviewer approval to merge; the platform repo does not.
"""
