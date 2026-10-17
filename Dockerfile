# The LOC-Arena images, run as nobody. `sandbox` is the sandbox's: its command server and what the agents' code
# needs, with nothing of the harness or the scenarios, so agent code cannot read how it is graded. `runner` is a
# sandbox that runs the company's own agents with pi. `engine`, the last stage and so what a plain
# `docker build .` gives, holds the engine, its scenarios and the company repos, for the gateway and the episode.
FROM python:3.12-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
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
FROM node:22.20.0-bookworm-slim AS pi
RUN npm install --global --prefix /opt/pi @earendil-works/pi-coding-agent@0.99.2 && npm cache clean --force

# Its command server serves the agents service alone and runs pi in an empty /workspace. pi calls nothing but the
# gateway: it neither checks for a newer release nor refreshes its model catalog.
FROM sandbox AS runner
USER root
COPY --from=pi /usr/local/bin/node /usr/local/bin/node
COPY --from=pi /opt/pi /opt/pi
RUN ln -s /app/sandbox_server/pi_on_gateway.py /usr/local/bin/pi-on-gateway && mkdir /workspace && chown nobody /workspace
ENV PATH=/opt/pi/bin:$PATH PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0
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
# is recreated empty with its container, which is what gives the honest twin a fresh IAM.
RUN uv sync --frozen --no-dev && mkdir /sealed /output /checkouts /var/lib/iam && chown nobody /sealed /output /checkouts /var/lib/iam
USER nobody
