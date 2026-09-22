#!/usr/bin/env bash
# scripts/down.sh RUN=aurora-efficiency -- tear the stack down (alias of teardown).
set -euo pipefail
exec "$(dirname "$0")/teardown.sh"
