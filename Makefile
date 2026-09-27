# Makefile -- one command to run everything (no live server; the viewer is a static report).
# Targets call loc_arena/cli.py.
#
# STACK=1 additionally brings the per-episode docker stack up/down around the run for the sealed-isolation
# guarantee. The episode itself runs in-process and reproducibly, so the stack is optional;
# on a Linux host it can be the default.

# The DELIVERABLE run is live-model only (aurora-efficiency.yaml, policy: model; needs OPENROUTER_API_KEY).
# aurora-efficiency.yaml (scripted) is retained only as deterministic engine-test scaffolding, not a deliverable policy.
RUN ?= aurora-efficiency
MODE ?= attack
HONEST ?= 2
ATTACK ?= 2
STACK ?= 0

.PHONY: run view sweep help gates setup public-repo

help:
	@echo "make setup                                # check toolchain, sync deps, validate config"
	@echo "make public-repo                          # build the curated public tree into dist/public-repo (verified)"
	@echo "make run RUN=aurora-efficiency MODE=attack|honest  # live-model episode, write the audit bundle"
	@echo "make view RUN=aurora-efficiency                       # open the latest report.html + inspect view"
	@echo "make sweep RUN=aurora-efficiency HONEST=n ATTACK=m  # n honest + m attack episodes, aggregated"
	@echo "make gates                                # ruff + ruff format + ty + pytest"
	@echo "  (append STACK=1 to run/sweep to bring the docker stack up/down around it)"

setup:
	@command -v uv >/dev/null || (echo "uv not found: install from https://docs.astral.sh/uv/" && exit 1)
	@uv sync
	@uv run python -c "from loc_arena.config import load_run_config as L; from loc_arena.monitors.registry import load_monitor_specs as S, firewall_violations as F; L('configs/aurora-efficiency.deterministic.yaml'); L('configs/aurora-efficiency.yaml'); v=F(S('configs/monitors.yaml')); assert not v, v; print('config OK: aurora-efficiency live + deterministic + monitors.yaml (firewall clean)')"
	@(command -v docker >/dev/null && docker info >/dev/null 2>&1 && echo "docker: available (used for the sealed-isolation stack)") || echo "docker: optional (the tests run without it)"
	@echo "setup complete. Set OPENROUTER_API_KEY in .env, then:  make run"

run:
ifeq ($(STACK),1)
	@uv run python -c "from loc_arena.config import load_run_config as L; from loc_arena.harness import up,teardown; s=up(L('configs/$(RUN).yaml'),project='locarena-$(RUN)'); print('stack up'); \
import loc_arena.cli as c; c.main(['run','--run','$(RUN)','--mode','$(MODE)']); teardown(s); print('stack down')"
else
	@uv run python -m loc_arena.cli run --run $(RUN) --mode $(MODE)
endif

view:
	@uv run python -m loc_arena.cli view --run $(RUN)

sweep:
	@uv run python -m loc_arena.cli sweep --run $(RUN) --honest $(HONEST) --attack $(ATTACK)

public-repo:
	@uv run python scripts/make_public.py

gates:
	@uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q
