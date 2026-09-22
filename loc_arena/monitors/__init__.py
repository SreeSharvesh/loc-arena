"""Configurable monitors package (the firewall; config over code).

Owns the model-backed monitors and the deterministic detectors, wired entirely from configs/monitors.yaml to
tap points. Importing this package registers every implementation so ``registry.load_monitors`` can wire the
enabled ones. A monitor reads only mirror/observable surfaces plus the fixed deploy-time baseline, never the
sealed recompute; the firewall is CI-asserted against monitors.yaml.
"""

from loc_arena.monitors import (  # noqa: F401  (register on import)
    action_monitor,
    detectors,
    inference_monitor,
    pr_review_monitor,
)
