#!/usr/bin/env bash
# scripts/run.sh [RUN] [MODE] -- run one episode and write its audit bundle (loc_arena.cli run).
set -euo pipefail
RUN="${1:-aurora-efficiency}"
MODE="${2:-attack}"
exec uv run python -m loc_arena.cli run --run "$RUN" --mode "$MODE"
