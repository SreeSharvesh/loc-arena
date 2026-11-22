"""Side-task registration for the Aurora scenario: importing binds its kill chains into the registry."""

from loc_arena.verifier.side_task import (  # noqa: F401 -- @register_verifier runs on import
    kill_chain_job_loop,
    kill_chain_standing_agent,
)
