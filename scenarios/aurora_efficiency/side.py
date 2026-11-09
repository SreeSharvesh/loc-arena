"""Side-task registration for the Aurora scenario: importing binds ``kill_chain_job_loop`` into the registry."""

from loc_arena.verifier.side_task import kill_chain_job_loop  # noqa: F401  -- @register_verifier runs on import
