# syntax=docker/dockerfile:1
# The LOC-Arena app image: the episode runner and the gateway_core provider endpoint (compose `image: app`).
# Pattern from Astral's uv Docker guide (docs.astral.sh/uv/guides/integration/docker): pinned uv, a
# dependency-only sync cached separately from the project, the venv on PATH. The dev group stays in: the
# runner runs the agents' pytest. .dockerignore is an allowlist, so .env, .venv and logs never enter the
# build context. /app stays root-owned so code running in the container cannot rewrite the harness or venv.
FROM python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
COPY --from=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 PYTHONUNBUFFERED=1

RUN groupadd --system --gid 999 nonroot \
 && useradd --system --gid 999 --uid 999 --no-log-init --create-home nonroot

WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

COPY pyproject.toml uv.lock README.md ./
COPY loc_arena/ loc_arena/
COPY company/ company/
COPY scenarios/ scenarios/
COPY configs/ configs/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked

ENV PATH="/app/.venv/bin:$PATH"
USER nonroot
CMD ["python", "-m", "loc_arena.cli", "--help"]
