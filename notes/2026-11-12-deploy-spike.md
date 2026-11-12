# Deploy spike: merged code reaches a running service (2026-11-12)

Written by Claude Code. A throwaway spike for part 2 of #257, a deployment mechanism.

<img alt="Merged code goes from the forge through a deployer into a release volume, and the service loads it after its supervisor restarts it" src="deploy-spike-path.svg" width="100%">

An agent changed the notes service's code, pushed it, and a teammate merged it. After one deploy the running
notes service answered with the new code. The spike changed no engine code and no company code:
`scripts/spike_deploy.py` patches the rendered compose file and drives the stack the Docker tests start
(`aurora-efficiency.stack-test`), with no provider key.

## The mechanism

Three additions to the compose file:

| Addition | What it does |
|---|---|
| A `releases` volume | Every service that serves company code mounts it read-only. A `deployer` container alone writes it. |
| `/releases/current/<repo>` first on `PYTHONPATH` | A service imports the released copy of a repo. Without a release it imports the image's copy, as today. |
| A supervisor as the container's command | It starts the service, and starts it again when `current` changes or when the service exits. |

A deploy is `git archive main` in the forge's container, unpacked as a new release directory, and one atomic
swap of the `current` link.

## Measured

| Step | Result |
|---|---|
| Edit, push, `open_pr`, `merge` by a second agent | Works with today's tools and rights |
| Write a release from the forge's `main` | 0.2 to 0.9 s |
| First answer from new code, one service, container restart | 3 s after the deploy began |
| First answer from new code, three services, restart in the container | 7 s after the deploy began |
| An MCP session held open through the tools gateway | Carries on in both cases: calls fail for 1 to 4 s, then the new code answers |
| Roll back a broken release | 4.4 s from the swap to the first good answer |

## Findings

| # | Finding | Evidence | What it means for a design |
|---|---|---|---|
| 1 | A restart of two containers can swap their addresses, and the tools gateway keeps the old ones. | `docker compose restart notes modelstore`: notes went from 172.20.0.5 to 172.20.0.13 and the model store took 172.20.0.5. The gateway then sent the caller's notes credential to the model store. Every new session got `401` until the gateway restarted. One service at a time kept its address in every try. | Restart the service's process in its container. With the supervisor, three services reloaded together and the gateway stayed correct. |
| 2 | State in memory is lost. | A note written before the deploy was gone after it. Chat kept its message, because its database is on disk. | Each service needs its state on disk before it can be redeployed. |
| 3 | The job service does not survive a restart. | It forgot a running job and a finished one. The next job got the id `job-4` a second time. Two seeded jobs became `failed`. The forgotten job's process still ran on `node-2`, and `get_job` answered `no job 'job-5'`. | Leave `meridian-jobsvc` out of the first version. A restart there also leaves a process that no record owns. |
| 4 | Broken code deploys. | A commit with a syntax error merged (`all required stages green; review satisfied`), deployed, and the service exited in a loop. Docker still showed the container `healthy` 10 s later. | A deploy needs its own health probe and a rollback. Real CI (step 3 of the forge plan) would stop this commit earlier. |
| 5 | One repo is five services. | A release of `meridian-controlplane` reloaded notes, chat and the model store together, and it also holds tickets and agents. | A deploy names a repo, so it reloads every service that imports it. |
| 6 | A service can write every service's record. | In the notes container, `/records/chat.jsonl`, `forge.jsonl` and `jobsvc.jsonl` are writable. All services run as the same user on one volume. | This is the concern #257 already names for service repos. Deployed agent code could change the records that monitors and the verifier read. |
| 7 | The company's inference server is a poor first target. | `meridian_serving.http` listens on its node's loopback, so only its own job reaches it. | A redeploy of it changes nothing an agent can observe. The services are where a deploy shows. |

## Not covered

- Real agent sessions (pi) during a deploy. The spike held one MCP session open with a test client.
- A `deploy` service under IAM: its right, its record, and its event for monitors.
- The honest twin: the release volume must start empty for it.
- The tickets, agents and eval services after a reload, beyond a check that they answer.
- Grading on what is deployed (part 3 of #257).

## Run it again

Build the images from this branch first, as the Docker tests need. `STATE` is any empty directory.

```sh
uv run python scripts/spike_deploy.py "$STATE" up notes chat forge sandbox-serving-agent sandbox-datapipe-agent
uv run python scripts/spike_deploy.py "$STATE" tool serving-agent write_note '{"key":"a","text":"b"}'
uv run python scripts/spike_deploy.py "$STATE" release meridian-controlplane   # after a merge on the forge
uv run python scripts/spike_deploy.py "$STATE" restart notes                   # the container restart variant
uv run python scripts/spike_deploy.py "$STATE" down
```

`up` writes the play's secrets to `$STATE/state.json`; `down` removes the project, and the file can go with it.
The supervisor variant is not in the script: the spike added it to `$STATE/compose.yaml` by hand, as the
container's command in front of the service's own (`python -u -c <this> <the service's command>`):

```python
import os, subprocess, sys, time
LINK = "/releases/current"
def target():
    try:
        return os.readlink(LINK)
    except OSError:
        return None
while True:
    seen = target()
    child = subprocess.Popen(sys.argv[1:])
    while child.poll() is None and target() == seen:
        time.sleep(0.25)
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(10)
        except subprocess.TimeoutExpired:
            child.kill(); child.wait()
    else:
        print(f"supervisor: the service exited {child.returncode}; starting it again", flush=True)
        time.sleep(1)
```
