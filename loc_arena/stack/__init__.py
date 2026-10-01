"""The isolated stack's shared foundation: the types the gateway's services and their clients share.

Every module here imports only the standard library, pydantic and :mod:`loc_arena.logging_.events`, so the
sandbox image can ship this package whole.
"""
