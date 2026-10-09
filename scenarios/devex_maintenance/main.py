"""Register the devex prototype scorer."""

from loc_arena.registry import register_scorer
from scenarios.maintenance import score

register_scorer("devex_ticket_completion")(score)
