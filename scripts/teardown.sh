#!/usr/bin/env bash
# scripts/teardown.sh -- remove every resource labelled loc-arena.eval=1 (reliable, idempotent).
set -euo pipefail
ids=$(docker ps -aq --filter "label=loc-arena.eval=1" || true)
[ -n "$ids" ] && docker rm -f $ids || echo "no loc-arena containers"
docker network ls -q --filter "label=loc-arena.eval=1" | xargs -r docker network rm 2>/dev/null || true
docker volume ls -q --filter "label=loc-arena.eval=1" | xargs -r docker volume rm 2>/dev/null || true
echo "teardown complete"
