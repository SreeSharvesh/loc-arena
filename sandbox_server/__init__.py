"""The sandbox's command server: runs one command at a caller's request, in a checkout, for a token holder.

It imports nothing of the harness (``loc_arena``, ``scenarios``), so a sandbox image can run it without the
grading code. ``loc_arena`` imports the wire types and the runner from here, never the other way round.
"""
