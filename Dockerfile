# The LOC-Arena image: the engine, its scenarios and its dependencies, run as an unprivileged user.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY loc_arena ./loc_arena
COPY scenarios ./scenarios
RUN uv sync --frozen --no-dev && mkdir /sealed && chown nobody /sealed
USER nobody
