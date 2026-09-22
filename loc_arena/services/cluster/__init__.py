"""Cluster / job-queue service (tamperable).

Enforces the sealed-vs-tamperable isolation.

Owns job submission and execution on the simulated cluster, where training/eval jobs and any persistent
worker run. The job census is a capture surface for the verifier.
"""
