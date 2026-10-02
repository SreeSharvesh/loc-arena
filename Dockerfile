# syntax=docker/dockerfile:1
# The sandbox image, built from uv.lock (Docker "Multi-stage builds": named stages, `--target`, a stage built
# FROM an earlier one):
# - `sandbox` (loc-arena-sandbox): each agent's sandbox (the execution app), the gateway edge and the grader.
#   Only what they import: company/, loc_arena/{stack,execution,grader}, gateway/edge.py, logging_/events.py
#   and the package __init__ files; no configs/, no scenarios/ (sealed reference), no live.py (covert briefs).
# Pattern from Astral's uv Docker guide (docs.astral.sh/uv/guides/integration/docker): pinned uv, a
# dependency-only sync cached separately from the project, the system Python in every stage
# (UV_PYTHON_DOWNLOADS=0), the venv on PATH. The dev group stays in: agents and the grader run pytest.
# .dockerignore is an allowlist, so .env, .venv and logs never enter the build context. /app stays
# root-owned so code running in a container cannot rewrite the harness or the venv.
FROM python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 PYTHONUNBUFFERED=1

RUN groupadd --system --gid 999 nonroot \
 && useradd --system --gid 999 --uid 999 --no-log-init --create-home nonroot

# The mount point of every named volume (sealed_log, mirror_log, checkout), owned by nonroot in every image.
# Docker fills an empty volume from the image at its first mount ("Mounting a volume over existing data"),
# and that copy gives the volume's root the mount point's owner and mode (moby CopyImagePathContent calls
# containerd continuity fs.CopyDir, which applies them to the destination root). So whichever image's
# container first mounts a fresh volume, the services writing it, all uid 999, can write it.
RUN install --directory --owner=nonroot --group=nonroot /workspace /sealed /mirror

WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project
ENV PATH="/app/.venv/bin:$PATH"

# The project is not installed here: `python -m` and uvicorn (--app-dir defaults to the working directory)
# import loc_arena from WORKDIR /app.
FROM base AS sandbox
COPY company/ company/
COPY loc_arena/__init__.py loc_arena/
COPY loc_arena/stack/ loc_arena/stack/
COPY loc_arena/execution/ loc_arena/execution/
COPY loc_arena/grader/ loc_arena/grader/
COPY loc_arena/gateway/__init__.py loc_arena/gateway/edge.py loc_arena/gateway/
COPY loc_arena/logging_/__init__.py loc_arena/logging_/events.py loc_arena/logging_/
USER nonroot
