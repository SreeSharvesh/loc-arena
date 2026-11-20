"""Side-task registration for the Aurora scenario: importing binds its kill chain into the registry."""

from loc_arena.verifier.rogue_agent_swarm import (  # noqa: F401 -- @register_verifier runs on import
    rogue_agent_swarm,
)
from loc_arena.verifier.side_task import (  # noqa: F401 -- @register_verifier runs on import
    kill_chain_job_loop,
)
