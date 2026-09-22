"""Event logging and audit-view package.

Enforces fingerprinted, append-only logging and the sealed-vs-mirror split.

Owns the single writer path for every event (so fingerprinting and append-only semantics are guaranteed in
one place) and the static multi-agent viewer that reconstructs each agent's trajectory from the sealed log.
"""
