"""The networkless grader: measures one checkout, without the reference, for the host to score.

``measurement`` runs the suites and the sealed benchmark; ``python -m loc_arena.grader`` prints the result.
The package imports only :mod:`loc_arena.stack`, :mod:`loc_arena.execution` (its checkout helpers), the
standard library and third-party packages, so it ships in the sandbox image.
"""
