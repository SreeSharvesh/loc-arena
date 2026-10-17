#!/usr/bin/env python3
"""Run pi on a model through the gateway: ``pi-on-gateway <model> [pi's arguments]``.

pi reads a provider's URL from its ``models.json`` alone, never from a variable, and a runner's reset empties
pi's home: so each run writes the gateway provider there afresh, from ``LOC_ARENA_GATEWAY_API``, then becomes
pi.
"""

import json
import os
import sys
from pathlib import Path

PROVIDER = "gateway"


def main() -> None:
    """Write the gateway provider with ``model`` on it into pi's home, then run pi on that model."""
    model, arguments = sys.argv[1], sys.argv[2:]
    agent_directory = Path.home() / ".pi" / "agent"
    agent_directory.mkdir(parents=True, exist_ok=True)
    provider = {
        "baseUrl": os.environ["LOC_ARENA_GATEWAY_API"],
        "api": "openai-completions",
        "apiKey": "replaced-by-the-gateway",
        "models": [{"id": model}],
    }
    (agent_directory / "models.json").write_text(json.dumps({"providers": {PROVIDER: provider}}))
    os.execvp("pi", ["pi", "--model", f"{PROVIDER}/{model}", *arguments])


if __name__ == "__main__":
    main()
