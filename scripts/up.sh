#!/usr/bin/env bash
# scripts/up.sh RUN=aurora-efficiency -- bring a fresh per-episode stack up healthy; scripts/down.sh tears it down.
set -euo pipefail
RUN="${RUN:-aurora-efficiency}"
uv run python -c "from loc_arena.compose_stack import name_compose_project, up; from loc_arena.harness import load_config_by_name; s=up(load_config_by_name('${RUN}'), project=name_compose_project('${RUN}')); print('up:', s.project, s.compose_file)"
