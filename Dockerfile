# The LOC-Arena image: the engine, the sandbox's command server, its scenarios, the company repos and the
# dependencies, run as nobody.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH HOME=/tmp
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
