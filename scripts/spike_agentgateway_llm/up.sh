#!/usr/bin/env bash
# Render <config>.yaml (YAML anchors and x- keys resolved, since agentgateway refuses unknown top-level keys),
# validate it, and (re)start the mock upstream and agentgateway on the agwspike network, gateway on :18080.
set -euo pipefail
cd "$(dirname "$0")"
config=${1:-parity}
python=${PYTHON:-../../.venv/bin/python}
[ -x "$python" ] || python=/Users/abo-tresol/Documents/ai-safety/spar-in-the-wild-ai-control/external/loc-arena/.venv/bin/python
"$python" -c "
import sys, yaml
d = yaml.safe_load(open(sys.argv[1] + '.yaml'))
for k in [k for k in d if k.startswith('x-')]: d.pop(k)
open(sys.argv[1] + '.gen.yaml', 'w').write(yaml.safe_dump(d, sort_keys=False))
" "$config"
image=ghcr.io/agentgateway/agentgateway:v1.6.0
docker network create agwspike >/dev/null 2>&1 || true
docker run --rm -v "$PWD/$config.gen.yaml:/c.yaml:ro" -v "$PWD/secrets:/run/secrets:ro" "$image" -f /c.yaml --validate-only
docker rm -f agwspike-mock agwspike-gw >/dev/null 2>&1 || true
docker run -d --name agwspike-mock --network agwspike -v "$PWD/mock_upstream.py:/m.py:ro" python:3.12-slim python -u /m.py >/dev/null
docker run -d --name agwspike-gw --network agwspike -p 127.0.0.1:18080:8080 \
  -v "$PWD/$config.gen.yaml:/c.yaml:ro" -v "$PWD/secrets:/run/secrets:ro" "$image" -f /c.yaml >/dev/null
sleep 2
