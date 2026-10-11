"""Spike only: bring up a reduced stack of the spike config with agent-main's sandbox on the harness image.

Usage: ``uv run python scripts/spike_harness/up.py <project> <compose file>``, with the provider key in the
environment (``set -a; . <.env>; set +a``). The images must be built first under the tags printed by
``--tags``. It renders the stack's compose file as a run does, then: every image under this spike's own tags,
no ``build`` (the images are built beforehand), agent-main's sandbox on the harness image, and no episode and
no notes board (neither is needed, and notes' image tag is shared with every other checkout). It starts the
gateway, two sandboxes and the tools gateway, which pulls in the tool services. Secrets reach compose through
its environment alone; nothing secret is written to disk.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import yaml
from loc_arena.config import load_run_config
from loc_arena.episode_stack import REPOSITORY, _issue_secrets, render_compose

RUN = "aurora-efficiency.spike"
TAG = "spike-harness"
IMAGES = {
    "image": f"loc-arena:{TAG}",
    "sandbox_image": f"loc-arena-sandbox:{TAG}",
}
HARNESS_IMAGE = f"loc-arena-sandbox-harness:{TAG}"
HARNESS_AGENT = "agent-main"
STARTED = ["gateway", f"sandbox-{HARNESS_AGENT}", "sandbox-serving-agent", "agentgateway"]
DROPPED = ["episode", "notes"]


def main() -> None:
    if sys.argv[1:] == ["--tags"]:
        print(IMAGES["image"], IMAGES["sandbox_image"], HARNESS_IMAGE)
        return
    project, compose_file = sys.argv[1], Path(sys.argv[2])
    loaded = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
    stack = loaded.settings.stack.model_copy(update=IMAGES)
    config = dataclasses.replace(loaded, settings=loaded.settings.model_copy(update={"stack": stack}))
    document = render_compose(config, REPOSITORY, RUN, [])
    services = document["services"]
    for name in DROPPED:
        del services[name]
    for service in services.values():
        service.pop("build", None)
        service["pull_policy"] = "never" if "image" in service and "agentgateway" not in service["image"] else "missing"
        for name in DROPPED:
            service.get("depends_on", {}).pop(name, None)
    services[f"sandbox-{HARNESS_AGENT}"]["image"] = HARNESS_IMAGE
    compose_file.write_text(yaml.safe_dump(document))
    attack_secrets, _ = _issue_secrets(config)
    compose = ["docker", "compose", "-p", project, "-f", str(compose_file), "--project-directory", str(compose_file.parent)]
    subprocess.run([*compose, "up", "--detach", "--wait", *STARTED], env={**os.environ, **attack_secrets}, check=True)


if __name__ == "__main__":
    main()
