#!/usr/bin/env bash
# scripts/teardown.sh -- remove every loc-arena.eval=1 resource, episode image and control key (idempotent).
set -euo pipefail
ids=$(docker ps -aq --filter "label=loc-arena.eval=1" || true)
[ -n "$ids" ] && docker rm -f $ids || echo "no loc-arena containers"
docker network ls -q --filter "label=loc-arena.eval=1" | xargs -r docker network rm 2>/dev/null || true
docker volume ls -q --filter "label=loc-arena.eval=1" | xargs -r docker volume rm 2>/dev/null || true
# Each episode's images are tagged with its project name; removed by name, an image another tag names stays.
docker image ls --filter "reference=loc-arena-*:locarena-*" --format '{{.Repository}}:{{.Tag}}' |
  xargs -r docker image rm 2>/dev/null || true
# Control keys a killed harness left in Python's temp directory (tempfile.gettempdir's order).
find "${TMPDIR:-${TEMP:-${TMP:-/tmp}}}" -maxdepth 1 -type d -name 'locarena-key-*' -user "$(id -u)" \
  -exec rm -rf {} + 2>/dev/null || true
echo "teardown complete"
