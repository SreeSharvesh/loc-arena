"""Where agent-written code runs: each agent's code tools, including a real shell, in its own sandbox.

``workspace`` runs the tools over a company checkout, ``app`` serves them in the sandbox, ``client`` is the
runner's side, and ``checkout`` builds and runs a checkout for the host, the sandboxes and the grader. The
package imports only :mod:`loc_arena.stack`, :mod:`loc_arena.logging_.events`, the standard library and
third-party packages, so it ships in the sandbox image (no configs/, scenarios/ or live.py there).
"""
