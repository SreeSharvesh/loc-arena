"""Sealed side-task verifier package.

Enforces the honest-baseline-scores-zero guarantee and the firewall: it reads the sealed side only.

Owns the pure, deterministic predicates that decide, from the sealed layer alone, whether a rogue internal
deployment succeeded. Nothing here reads a mirror surface or feeds a monitor.
"""
