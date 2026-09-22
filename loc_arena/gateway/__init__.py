"""Inference gateway package.

Enforces mandatory inference routing and the sealed-vs-tamperable isolation.

Owns the only path to a model: a tamperable edge the agents point at and a sealed core that holds the single
provider key, is the only egress, and writes the sealed generate record. There is no other route to
inference.
"""
