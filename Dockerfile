# The LOC-Arena images, run as nobody. `sandbox` is the sandbox's: its command server and what the agents' code
# needs, with nothing of the harness or the scenarios, so agent code cannot read how it is graded. `engine`, the
# last stage and so what a plain `docker build .` gives, holds the engine, its scenarios and the company repos,
# for the gateway and the episode.
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

FROM base AS engine
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY loc_arena ./loc_arena
COPY sandbox_server ./sandbox_server
COPY scenarios ./scenarios
COPY company ./company
# The gateway's call log, the episode's bundles and the checkouts the episode shares with the sandbox: named
# volumes take their owner from these directories.
RUN uv sync --frozen --no-dev && mkdir /sealed /output /checkouts && chown nobody /sealed /output /checkouts
USER nobody
