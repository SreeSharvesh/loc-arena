"""Weights / artifact store (tamperable).

Enforces the sealed-vs-tamperable isolation; the exfil guard.

Owns the checkpoint and artifact store with attributed reads and a bandwidth-limited egress (the
weights-egress guard). Reads are recorded on the sealed side for the verifier's cross-check.
"""
