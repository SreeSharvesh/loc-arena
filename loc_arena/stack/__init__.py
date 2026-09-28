"""The isolated stack's shared foundation: contracts, settings, constants, the HTTP client and the secrets.

It also holds the model-call types the gateway's services share (``model_call.py``). Every module here
imports only the standard library, pydantic, pydantic-settings, FastAPI, httpx and
:mod:`loc_arena.logging_.events`, so the sandbox image can ship this package whole.
"""
