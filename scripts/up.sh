#!/usr/bin/env bash
# scripts/up.sh RUN=aurora-efficiency -- bring the per-episode stack up healthy (loc_arena.harness.up).
set -euo pipefail
RUN="${RUN:-aurora-efficiency}"
uv run python -c "from loc_arena.config import load_run_config; from loc_arena.harness import up; s=up(load_run_config('configs/${RUN}.yaml'), project='locarena-${RUN}'); print('up:', s.project, s.compose_file)"
