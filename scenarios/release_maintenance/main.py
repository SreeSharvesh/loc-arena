"""Register the release prototype scorer."""

from loc_arena.registry import register_scorer
from scenarios.maintenance import score

register_scorer("release_ticket_completion")(score)
