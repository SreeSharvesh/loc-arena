#!/usr/bin/env bash
# scripts/teardown.sh -- remove every resource labelled loc-arena.eval=1 and every episode's images (idempotent).
set -euo pipefail
ids=$(docker ps -aq --filter "label=loc-arena.eval=1" || true)
[ -n "$ids" ] && docker rm -f $ids || echo "no loc-arena containers"
docker network ls -q --filter "label=loc-arena.eval=1" | xargs -r docker network rm 2>/dev/null || true
docker volume ls -q --filter "label=loc-arena.eval=1" | xargs -r docker volume rm 2>/dev/null || true
# Each episode's images carry its project name as their tag (locarena-...). Removed by name, so an image that
# another tag also names is only untagged (docs.docker.com/reference/cli/docker/image/ls, the reference filter).
docker image ls --filter "reference=loc-arena-*:locarena-*" --format '{{.Repository}}:{{.Tag}}' |
  xargs -r docker image rm 2>/dev/null || true
echo "teardown complete"
