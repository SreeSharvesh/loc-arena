#!/usr/bin/env bash
# scripts/teardown.sh -- remove every resource labelled loc-arena.eval=1, every episode's images and control keys
# (idempotent).
set -euo pipefail
ids=$(docker ps -aq --filter "label=loc-arena.eval=1" || true)
[ -n "$ids" ] && docker rm -f $ids || echo "no loc-arena containers"
docker network ls -q --filter "label=loc-arena.eval=1" | xargs -r docker network rm 2>/dev/null || true
docker volume ls -q --filter "label=loc-arena.eval=1" | xargs -r docker volume rm 2>/dev/null || true
# Each episode's images are tagged with its project name; removed by name, an image another tag names stays.
docker image ls --filter "reference=loc-arena-*:locarena-*" --format '{{.Repository}}:{{.Tag}}' |
  xargs -r docker image rm 2>/dev/null || true
# Each episode's control key is a file in a directory of its own, which the harness makes in Python's temp
# directory ($TMPDIR, else $TEMP, else $TMP, else /tmp: docs.python.org/3/library/tempfile.html#tempfile.gettempdir)
# and its teardown deletes; a harness killed before its teardown leaves it behind.
find "${TMPDIR:-${TEMP:-${TMP:-/tmp}}}" -maxdepth 1 -type d -name 'locarena-key-*' -user "$(id -u)" \
  -exec rm -rf {} + 2>/dev/null || true
echo "teardown complete"
