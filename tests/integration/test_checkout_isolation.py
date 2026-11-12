"""Each agent's checkout is its own: an edit in it reaches no other agent, no other sandbox and no job.

The rendered compose project is brought up with a sandbox per agent, the forge, and the job service with its
nodes, with one play's secrets. In the episode container every agent's checkout is cloned from the forge as a
run clones it, and agent-main edits its own and pushes nothing: its native `write_file` makes a new file and
its `bash` appends to a tracked one, each holding a random marker. Then every other agent looks for the
marker. Its `bash` walks the whole filesystem of its sandbox. Its file tools read agent-main's new file by
the same path, by its absolute path and by a path climbing out of its own checkout, and search its checkout.
One of them submits a job that walks its node's filesystem the same way and checks that the edited file is
there as `main` holds it. agent-main's own tools and sandbox finding the marker are the control of every "not
found". Each walk gets the marker in two halves, so no command line or process record holds it whole.
Skipped unless the Docker daemon answers and the stack image exists.
"""

from __future__ import annotations

import dataclasses
import json
import os
import secrets
import subprocess
from dataclasses import dataclass
from typing import Any

import pytest
import yaml
from loc_arena.config import load_run_config, sandbox_service
from loc_arena.episode_stack import (
    CONFIGS_DIRECTORY,
    EPISODE_SERVICE,
    REPOSITORY,
    issue_secrets,
    render_compose,
)
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.sandbox import IDENTITY_PREFIX

from tests.integration._docker_support import image_exists

RUN = "aurora-efficiency.stack-test"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
EDITOR = "agent-main"
OTHERS = [agent.id for agent in CONFIG.agents if agent.id != EDITOR]
SUBMITTER = "serving-agent"  # another agent, one who may submit jobs
WRITTEN = "meridian-serving/meridian_serving/isolation_note.py"  # new, made with write_file
EDITED = "meridian-serving/meridian_serving/__init__.py"  # tracked, appended to with bash
MARKER = secrets.token_hex(16)
JOBSVC = next(service for service in CONFIG.live_services if service.name == "jobsvc")
# Under tags of this test alone: compose recreates a running service whose image tag another checkout's build
# moved.
OWN_IMAGES = {
    field: f"{getattr(CONFIG.settings.stack, field)}-isolation-{secrets.token_hex(3)}"
    for field in ("image", "sandbox_image", "worker_image")
}
STACK_CONFIG = dataclasses.replace(
    CONFIG,
    settings=CONFIG.settings.model_copy(
        update={"stack": CONFIG.settings.stack.model_copy(update=OWN_IMAGES)},
    ),
)

pytestmark = pytest.mark.skipif(
    not image_exists(CONFIG.settings.stack.image),
    reason="needs a reachable Docker daemon and the stack image",
)

# Run in a sandbox or as a job: every file of the filesystem holding the marker its two halves make, and which
# of the paths after them are files. What its user cannot read is out of its reach too.
LOOK = """
import json, os, sys
marker = (sys.argv[1] + sys.argv[2]).encode()
found = []
for top, directories, files in os.walk("/"):
    directories[:] = [d for d in directories if os.path.join(top, d) not in ("/proc", "/sys", "/dev")]
    for name in files:
        path = os.path.join(top, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as file:
                tail = b""
                while block := file.read(1 << 20):
                    if marker in tail + block:
                        found.append(path)
                        break
                    tail = block[1 - len(marker):]
        except OSError:
            pass
print(json.dumps({"found": sorted(found), "holds": [path for path in sys.argv[3:] if os.path.isfile(path)]}))
"""
# Run in the submitter's sandbox, as its agent would: submit the command with its own identity on the job
# service, wait for the job to end, and print its state and output.
SUBMIT = """
import json, sys, time, urllib.request
url, identity_file, command = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
identity = open(identity_file).read().strip()
def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": f"Bearer {identity}", "Content-Type": "application/json"}
    request = urllib.request.Request(url + path, data, headers, method=method)
    with urllib.request.urlopen(request, timeout=30) as reply:
        return json.loads(reply.read())
job = call("POST", "/jobs", {"name": "isolation-look", "command": command})
while (job := call("GET", f"/jobs/{job['id']}"))["state"] not in ("succeeded", "failed", "cancelled"):
    time.sleep(0.5)
print(json.dumps({"state": job["state"], **call("GET", f"/jobs/{job['id']}/logs")}))
"""
# In the episode: every checkout cloned as a run clones it, agent-main's edits, then what each agent finds.
IN_EPISODE = r"""
import json, os, shlex, sys
from pathlib import Path
from loc_arena.checkouts import open_checkouts
from loc_arena.config import load_run_config
from loc_arena.sandbox import connect_sandboxes
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.tasks.main_task_grader import COMPANY_REPOS

given = json.load(sys.stdin)
editor, written, edited, marker = given["editor"], given["written"], given["edited"], given["marker"]
config = load_run_config(given["config"])
sandboxes = connect_sandboxes(config.settings, [agent.id for agent in config.agents])
checkouts = open_checkouts(config, sandboxes)
services = CodeServices(checkouts=checkouts, repos=COMPANY_REPOS, stack=config.settings.stack)
halves = [marker[: len(marker) // 2], marker[len(marker) // 2 :]]

def run(agent, tool, **args):
    return services.run(tool, {**args, "actor_uid": agent})

def look(agent):
    command = shlex.join(["python", "-c", given["look"], *halves])
    return json.loads(run(agent, "bash", command=command)["stdout"])

def search(agent):
    return sorted({match["file"] for match in run(agent, "search_code", pattern=marker)["matches"]})

def read(agent, path):
    found = run(agent, "read_file", path=path)
    return found["content"] if "content" in found else found["error"].rsplit(": ", 1)[0]

run(editor, "write_file", path=written, content=f"NOTE = {marker!r}\n")
run(editor, "bash", command=f"echo '# {marker}' >> {edited}")
editors_file = checkouts[editor].directory / written
job = json.dumps(["python", "-c", given["look"], *halves, f"meridian/{edited}"])
submit = shlex.join(["python", "-c", given["submit"], given["jobsvc"], given["identity"], job])
submitted = run(given["submitter"], "bash", command=submit)

def paths_to_editors_file(agent):
    return [written, str(editors_file), os.path.relpath(editors_file, checkouts[agent].directory)]

print(json.dumps({
    "checkout": str(checkouts[editor].directory),
    "editor": {"read": read(editor, written), "search": search(editor), "walk": look(editor)["found"]},
    "others": {
        agent: {
            "walk": look(agent)["found"],
            "search": search(agent),
            "reads": [read(agent, path) for path in paths_to_editors_file(agent)],
        }
        for agent in given["others"]
    },
    "job": json.loads(submitted["stdout"]),
}))
"""


@dataclass(frozen=True)
class Looked:
    """agent-main's checkout, and what agent-main, each other agent and the job found of its edit."""

    checkout: str
    editor: dict[str, Any]
    others: dict[str, dict[str, Any]]
    job: dict[str, Any]


@pytest.fixture(scope="module")
def looked(tmp_path_factory: pytest.TempPathFactory) -> Looked:
    directory = tmp_path_factory.mktemp("isolation")
    (directory / "compose.yaml").write_text(yaml.safe_dump(render_compose(STACK_CONFIG, REPOSITORY, RUN, [])))
    attack, _ = issue_secrets(STACK_CONFIG)
    project = f"locarena-isolation-{secrets.token_hex(3)}"
    compose = ["docker", "compose", "-p", project, "-f", str(directory / "compose.yaml")]
    compose += ["--project-directory", str(directory)]
    environment = {**os.environ, API_KEY_VARIABLE: secrets.token_urlsafe(24), **attack}

    def run(*arguments: str, standard_input: str | None = None) -> str:
        done = subprocess.run(
            [*compose, *arguments],
            env=environment,
            input=standard_input,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, f"{' '.join(arguments[:4])}: {done.stderr[-4000:]}"
        return done.stdout

    sandboxes = [sandbox_service(agent.id) for agent in CONFIG.agents]
    given = {
        "config": f"{CONFIGS_DIRECTORY}/{RUN}.yaml",
        "editor": EDITOR,
        "others": OTHERS,
        "submitter": SUBMITTER,
        "written": WRITTEN,
        "edited": EDITED,
        "marker": MARKER,
        "look": LOOK,
        "submit": SUBMIT,
        "jobsvc": f"http://{JOBSVC.name}:{JOBSVC.port}",
        "identity": str(CONFIG.settings.gateway.secrets_dir / f"{IDENTITY_PREFIX}{JOBSVC.name}"),
    }
    try:
        run("build", JOBSVC.name, "node-1", sandboxes[0])
        run("up", "--detach", "--wait", "--no-build", JOBSVC.name, "forge", *sandboxes)
        played = run(
            *("run", "--no-deps", "--rm", "--use-aliases", "-T", EPISODE_SERVICE, "python", "-c", IN_EPISODE),
            standard_input=json.dumps(given),
        )
    finally:
        down = [*compose, "down", "--volumes", "--remove-orphans"]
        subprocess.run(down, env=environment, capture_output=True, check=False)
        subprocess.run(["docker", "image", "rm", *OWN_IMAGES.values()], capture_output=True, check=False)
    return Looked(**json.loads(played.strip().splitlines()[-1]))


def test_agent_main_finds_its_edit_with_its_own_tools_and_in_its_own_sandbox(looked: Looked) -> None:
    assert (looked.editor["read"], looked.editor["search"], looked.editor["walk"]) == (
        f"NOTE = {MARKER!r}\n",
        sorted([WRITTEN, EDITED]),
        sorted(f"{looked.checkout}/{path}" for path in (WRITTEN, EDITED)),
    )


def test_no_other_agents_sandbox_holds_agent_mains_edit_anywhere(looked: Looked) -> None:
    assert {agent: found["walk"] for agent, found in looked.others.items()} == dict.fromkeys(OTHERS, [])


def test_no_other_agents_file_tools_read_or_find_agent_mains_edit(looked: Looked) -> None:
    escapes = "bad args for read_file: path escapes the checkout"
    # By the same path, its own checkout has no such file; by agent-main's absolute path or by climbing out of
    # its own checkout, the path is refused.
    expected = {"reads": ["not a file", escapes, escapes], "search": []}

    found = {agent: {key: seen[key] for key in expected} for agent, seen in looked.others.items()}
    assert found == dict.fromkeys(OTHERS, expected)


def test_a_job_another_agent_submits_holds_main_as_merged_and_none_of_the_edit(looked: Looked) -> None:
    assert looked.job["state"] == "succeeded", looked.job

    assert json.loads(looked.job["stdout"]) == {"found": [], "holds": [f"meridian/{EDITED}"]}
