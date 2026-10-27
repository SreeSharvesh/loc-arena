# The LOC-Arena images, run as nobody. `sandbox` is the sandbox's: its command server and what the agents' code
# needs, with nothing of the harness or the scenarios, so agent code cannot read how it is graded. `runner` is a
# sandbox that runs the company's own agents with pi; `worker` a sandbox that runs the job service's jobs as real
# processes through its node daemon. `engine`, the last stage and so what a plain `docker build .` gives, holds
# the engine, its scenarios and the company repos, for the gateway and the episode.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv
# pip 26.2.2 and uv 0.13.4 are the minimums that stop trusting the compromised emSign CAs; the wheel's hash
# keeps the upgrade safe from the old pip that fetches it.
RUN pip install --no-cache-dir "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH HOME=/tmp

FROM base AS sandbox
# Only the `sandbox` dependency group: uv omits the project and its dependencies. The lock and pyproject are
# bound for the sync alone, so the image keeps neither.
RUN --mount=type=bind,source=uv.lock,target=uv.lock --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --only-group sandbox
COPY sandbox_server ./sandbox_server
# The checkouts the episode shares with the sandbox: the named volume takes its owner from this directory.
RUN mkdir /checkouts && chown nobody /checkouts
USER nobody

# pi, the coding agent the runners run, installed with the dependencies its own shrinkwrap pins.
FROM node:22.23.4-bookworm-slim AS pi
RUN npm install --global --prefix /opt/pi @earendil-works/pi-coding-agent@0.99.2

# Its command server serves the agents service alone and runs pi in an empty /workspace. pi calls nothing but the
# gateway: it neither checks for a newer release nor refreshes its model catalog.
FROM sandbox AS runner
USER root
COPY --from=pi /usr/local/bin/node /usr/local/bin/node
COPY --from=pi /opt/pi /opt/pi
RUN ln -s /app/sandbox_server/pi_on_gateway.py /usr/local/bin/pi-on-gateway && mkdir /workspace && chown nobody /workspace
ENV PATH=/opt/pi/bin:$PATH PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0
USER nobody

# The job service's fleet: its node daemon (in sandbox_server) runs one job at a time as a real process in
# /workspace, which each job empties, beside the checkouts the stack mounts read-only. No pi, no harness. Its
# own deployment of the seven company repos, read-only at /opt/meridian, is what other teams' jobs run, so they
# never run an agent's edits; the platform's meridian-iam is not one of them.
FROM sandbox AS worker
USER root
COPY company/meridian-common /opt/meridian/meridian-common
COPY company/meridian-serving /opt/meridian/meridian-serving
COPY company/meridian-datapipe /opt/meridian/meridian-datapipe
COPY company/meridian-distill /opt/meridian/meridian-distill
COPY company/meridian-evalkit /opt/meridian/meridian-evalkit
COPY company/meridian-jobsvc /opt/meridian/meridian-jobsvc
COPY company/meridian-controlplane /opt/meridian/meridian-controlplane
RUN mkdir /workspace && chown nobody /workspace
USER nobody

FROM base AS engine
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY loc_arena ./loc_arena
COPY sandbox_server ./sandbox_server
COPY scenarios ./scenarios
COPY company ./company
# The gateway's call log, the episode's bundles and the checkouts the episode shares with the sandbox: named
# volumes take their owner from these directories. /var/lib/iam is central IAM's store: an anonymous volume there
# is recreated empty with its container, which is what gives the honest twin a fresh IAM. /var/lib/tickets and
# /var/lib/chat hold the tickets and chat services' databases, in the container's own layer, which a recreated
# container starts without.
RUN uv sync --frozen --no-dev && mkdir /sealed /output /checkouts /var/lib/iam /var/lib/tickets \
    /var/lib/chat && chown nobody /sealed /output /checkouts /var/lib/iam /var/lib/tickets /var/lib/chat
USER nobody
