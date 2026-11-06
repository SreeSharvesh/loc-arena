"""A stack run's plays: the episode and its honest twin in one container, and its live services' logs.

Between the two plays every container agent code could leave state in is recreated empty (``renew_services``),
and each logged service's log is added to its file in the run directory first, since a recreated container
starts a new log. ``copy_out`` makes each copy out of the compose project once.
"""

from __future__ import annotations

import contextlib
import functools
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from loc_arena.config import RunConfig, sandbox_service
from loc_arena.gateway.model_gateway import MODEL_GATEWAY_SERVICE, drain_record, start_gateway, stop_callers
from loc_arena.merged_main import EPISODE_PHASE, TWIN_PHASE, PlayedForge, read_stack_forge
from loc_arena.node_pool import list_node_services
from loc_arena.observability import PHOENIX_SERVICE, export_spans
from loc_arena.runner_pool import list_runner_services
from loc_arena.tools_gateway import TOOLS_GATEWAY_SERVICE, serves_tools

EPISODE_SERVICE = "episode"  # the agent loop's compose service: the one caller every sandbox serves
# Run in IAM's container: the deployer's credential, issued and used there so it never leaves IAM, submits the
# server's job and reads its logs until it has loaded; it exits non-zero if the job ends or the wait runs out.
DEPLOY = """
import json, sys, time, urllib.request
from meridian_iam.core import Iam
from meridian_iam.core.base import SCOPE_PREFIX
database, deployer, service, url, job, wait = sys.argv[1:7]
token = Iam(database, lambda: int(time.time())).issue_credential(deployer, [service, SCOPE_PREFIX + service])
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
def call(path, body=None):
    with urllib.request.urlopen(urllib.request.Request(url + path, body, headers), timeout=30) as reply:
        return json.loads(reply.read())
job_id, deadline = call("/jobs", job.encode())["id"], time.monotonic() + float(wait)
while "loaded" not in call(f"/jobs/{job_id}/logs")["stdout"]:
    state = call(f"/jobs/{job_id}")["state"]
    if state in ("failed", "succeeded", "cancelled") or time.monotonic() > deadline:
        sys.exit(f"the server's job {job_id} is {state} and has not loaded its model")
    time.sleep(1)
"""


class StackError(RuntimeError):
    """The episode's compose project could not deliver its logs."""


def list_logged_services(config: RunConfig) -> list[str]:
    """The containers whose log a stack run copies out and recreates before the twin.

    Each live service, each node of the job service's fleet (its daemon's process facts), and the tools
    gateway when it runs.
    """
    tools_gateway = [TOOLS_GATEWAY_SERVICE] if serves_tools(config) else []
    return [service.name for service in config.live_services] + list_node_services(config) + tools_gateway


def list_service_log_copies(
    config: RunConfig,
    compose: list[str],
    service_logs: Path,
) -> list[tuple[list[str], Path]]:
    """Each logged service's ``compose logs`` command, and the file in ``service_logs`` its output joins."""
    return [
        ([*compose, "logs", "--no-color", "--no-log-prefix", service], service_logs / f"{service}.log")
        for service in list_logged_services(config)
    ]


def renew_services(
    config: RunConfig,
    compose: list[str],
    service_logs: Path,
    environment: dict[str, str],
    twin_environment: dict[str, str],
) -> None:
    """Before the honest twin: every container agent code could leave state in, recreated empty.

    Those are each sandbox, each runner, each live service and the tools gateway. The model gateway is
    recreated too, so it admits the twin's runners and counts their calls afresh, once its record is drained;
    the Collector, and the record, run on. The episode stays. Each logged service's log so far is first added
    to its file in ``service_logs``, every copy or none, since a recreated container starts a new log, then
    Phoenix's spans, once no runner can add one. The recreate uses ``twin_environment``, whose identities and
    credentials are the twin's own.
    Raises ``StackError`` when a log cannot be copied or the record does not drain, so nothing is recreated,
    or when a renewed container does not come back.
    """
    renewed = [
        MODEL_GATEWAY_SERVICE,
        *(sandbox_service(agent.id) for agent in config.agents),
        *list_runner_services(config),
        *list_logged_services(config),
    ]
    # Every caller the twin renews stops first, so no call is in flight when the record is drained. Phoenix
    # calls no model, and runs on until its spans are exported.
    run = functools.partial(subprocess.run, env=environment)
    callers = [service for service in renewed[1:] if service != PHOENIX_SERVICE]
    if not drain_record(run, compose, config.settings, callers=callers):
        raise StackError("the gateway's record did not drain before the honest twin, so nothing was renewed")
    copies = list_service_log_copies(config, compose, service_logs)
    logged = [subprocess.run(command, env=environment, capture_output=True) for command, _ in copies]
    if failed := [into.name for (_, into), done in zip(copies, logged, strict=True) if done.returncode]:
        raise StackError(
            f"could not copy {', '.join(failed)} out before the honest twin, so nothing was renewed",
        )
    try:  # every file opened before any is written, so one that cannot be leaves the others untouched
        with contextlib.ExitStack() as files:
            outputs = [files.enter_context(into.open("ab")) for _, into in copies]
            for output, done in zip(outputs, logged, strict=True):
                output.write(done.stdout)
    except OSError as error:
        raise StackError(
            f"could not write the logs before the honest twin, so nothing was renewed: {error}",
        ) from error
    export_spans(run, compose, config, service_logs)
    recreate = ["up", "--detach", "--wait", "--force-recreate", "--renew-anon-volumes", "--no-deps"]
    recreated = subprocess.run([*compose, *recreate, *renewed], env=twin_environment).returncode == 0
    if not (
        recreated
        and start_gateway(functools.partial(subprocess.run, env=twin_environment), compose, config.settings)
    ):
        raise StackError(
            "the renewed containers did not come back for the honest twin: see compose's output above",
        )


def deploy_company_server(
    config: RunConfig,
    compose: list[str],
    run_compose: Callable[..., subprocess.CompletedProcess[bytes]],
) -> None:
    """Start the live services and submit the company's own inference server as its deployer, if it has one.

    The job runs ``meridian_serving.http`` with its health probe, from the checkout of main its node makes.
    It returns once the server has loaded its model. Raises ``StackError`` when the services do not start, or
    the server does not load within ``serving.load_seconds``.
    """
    serving = config.settings.serving
    job_service = next((service for service in config.live_services if service.nodes), None)
    if serving.deployer is None or job_service is None:
        return
    services = [service.name for service in config.live_services]
    if run_compose([*compose, "up", "--detach", "--wait", "--no-recreate", *services]).returncode != 0:
        raise StackError("the live services did not start for the company's server: see compose's output")
    server = ["python", "-m", "meridian_serving.http", "--model", serving.model]
    server += ["--probe-seconds", str(serving.probe_seconds)]
    job = {"name": f"{serving.model}-serving", "command": server}
    url = f"http://{job_service.name}:{job_service.port}"
    database = str(config.settings.iam.database)
    submit = ["exec", "-T", config.settings.iam.service, "python", "-c", DEPLOY, database, serving.deployer]
    deployed = [*compose, *submit, job_service.name, url, json.dumps(job), str(serving.load_seconds)]
    if run_compose(deployed).returncode != 0:
        raise StackError(f"{serving.deployer}'s server did not load: see compose's output")


def play_phases(
    config: RunConfig,
    compose: list[str],
    mode: str,
    service_logs: Path,
    environment: dict[str, str],
    twin_environment: dict[str, str],
) -> tuple[int, dict[str, PlayedForge]]:
    """Play the episode and, in attack mode, the honest twin in the same container after ``renew_services``.

    Before each play the company's own server is deployed (``deploy_company_server``).

    Never recreated, the container plays its image of the first play again, whatever its tag now names. The
    twin plays under ``twin_environment``, on containers recreated with its own credentials. Then the runners
    stop, so no execution runs past play and the records copied next hold the whole of each, and Phoenix's
    spans are exported. Returns the exit code of the last play, and what each play left on the forge
    (``read_stack_forge``), by play, read before the forge is renewed; none without a forge.
    """
    attached = ["--attach", EPISODE_SERVICE, "--exit-code-from", EPISODE_SERVICE]
    play = [*compose, "up", "--no-recreate", *attached, EPISODE_SERVICE]

    def read_forge(phase_environment: dict[str, str]) -> PlayedForge | None:
        return read_stack_forge(config, compose, functools.partial(subprocess.run, env=phase_environment))

    played: dict[str, PlayedForge | None] = {}
    played_in = environment
    deploy_company_server(config, compose, functools.partial(subprocess.run, env=played_in))
    exit_code = subprocess.run(play, env=played_in).returncode
    if exit_code == 0:
        played[EPISODE_PHASE] = read_forge(played_in)
    if exit_code == 0 and mode != "honest":
        renew_services(config, compose, service_logs, environment, twin_environment)
        played_in = twin_environment
        deploy_company_server(config, compose, functools.partial(subprocess.run, env=played_in))
        exit_code = subprocess.run(play, env=played_in).returncode
        if exit_code == 0:
            played[TWIN_PHASE] = read_forge(played_in)
    run = functools.partial(subprocess.run, env=played_in)
    if not stop_callers(run, compose, list_runner_services(config)):
        raise StackError("could not stop the runners at the end of play: see compose's output above")
    export_spans(run, compose, config, service_logs)
    return exit_code, {phase: forge for phase, forge in played.items() if forge is not None}


def copy_out(
    run_compose: Callable[..., subprocess.CompletedProcess[bytes]],
    command: list[str],
    into: Path | None = None,
    *,
    made: set[tuple[str, ...]],
) -> bool:
    """Whether compose ran ``command``, with what it prints added ``into`` that file when given.

    A copy in ``made`` was made already and is not made again: a log's lines would repeat, and the run
    directory would lose what was written into it since. One that succeeds now joins ``made``.
    """
    if tuple(command) in made:
        return True
    try:
        with into.open("ab") if into else contextlib.nullcontext() as output:
            done = run_compose(command, stdout=output).returncode == 0
    except OSError:  # a file that cannot be written is a failed copy: the others are still tried
        return False
    if done:
        made.add(tuple(command))
    return done
