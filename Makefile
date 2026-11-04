# Makefile -- one command to run everything (no live server; the viewer is a static report).
# Targets call loc_arena/cli.py.
#
# A run plays where its config needs. With central IAM on (the default) it plays in a stack: the episode in its own
# container with no key and no internet, beside the gateway (which holds the key), a sandbox per agent, IAM and
# the scenario's services. A config with IAM off (the deterministic fixture) plays in this process, where the
# agents' code can reach local files. STACK=1 or STACK=0 forces either; the wrong one is refused.

# The DELIVERABLE run is live-model only (aurora-efficiency.yaml, policy: model; needs OPENROUTER_API_KEY).
# aurora-efficiency.yaml (scripted) is retained only as deterministic engine-test scaffolding, not a deliverable policy.
RUN ?= aurora-efficiency
MODE ?= attack
HONEST ?= 2
ATTACK ?= 2
STACK ?=
SWEEP ?= aurora-efficiency.deterministic

LOGS ?= logs
FILE_SIZE_BASE ?= origin/main

.PHONY: run view explore sweep help gates setup public-repo

help:
	@echo "make setup                                # check toolchain, sync deps, validate config"
	@echo "make public-repo                          # build the curated public tree into dist/public-repo (verified)"
	@echo "make run RUN=aurora-efficiency MODE=attack|honest  # live-model episode, write the audit bundle"
	@echo "make view RUN=aurora-efficiency                       # open the latest report.html + inspect view"
	@echo "make explore [LOGS=logs]                  # build the run explorer over every run in LOGS and open it"
	@echo "make sweep SWEEP=aurora-efficiency.deterministic HONEST=n ATTACK=m  # n honest + m attack, in process"
	@echo "make gates                                # ruff + ruff format + ty + pytest"
	@echo "  (run plays in a stack when IAM is on, which needs Docker Engine 28 or later; STACK=1|0 forces it)"

setup:
	@command -v uv >/dev/null || (echo "uv not found: install from https://docs.astral.sh/uv/" && exit 1)
	@uv sync
	@uv run python -c "from loc_arena.config import load_run_config as L; from loc_arena.monitors.registry import load_monitor_specs as S, firewall_violations as F; L('configs/aurora-efficiency.deterministic.yaml'); L('configs/aurora-efficiency.yaml'); v=F(S('configs/monitors.yaml')); assert not v, v; print('config OK: aurora-efficiency live + deterministic + monitors.yaml (firewall clean)')"
	@(command -v docker >/dev/null && docker info >/dev/null 2>&1 && echo "docker: available (used for the sealed-isolation stack)") || echo "docker: optional (the tests run without it)"
	@echo "setup complete. Set OPENROUTER_API_KEY in .env, then:  make run"

run:
ifeq ($(STACK),1)
	@uv run python -m loc_arena.cli run --run $(RUN) --mode $(MODE) --stack
else ifeq ($(STACK),0)
	@uv run python -m loc_arena.cli run --run $(RUN) --mode $(MODE) --no-stack
else
	@uv run python -m loc_arena.cli run --run $(RUN) --mode $(MODE)
endif

view:
	@uv run python -m loc_arena.cli view --run $(RUN)

explore:
	@uv run python -m loc_arena.cli explore $(LOGS)

sweep:
	@uv run python -m loc_arena.cli sweep --run $(SWEEP) --honest $(HONEST) --attack $(ATTACK)

public-repo:
	@uv run python scripts/make_public.py

gates:
	@python3 scripts/check_file_size.py --base "$(FILE_SIZE_BASE)"
	@uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q -n auto --dist loadscope
