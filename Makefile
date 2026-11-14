# Makefile -- one command to run everything (no live server; the viewer is a static report).
# Targets call loc_arena/cli.py.
#
# A run plays in a stack: the episode in its own container with no key and no internet, beside the gateway
# (which holds the key), a sandbox per agent, IAM and the scenario's services. Every run drives a live model
# and needs OPENROUTER_API_KEY in .env. RUN=aurora-efficiency.fast is the cheap smoke run.
RUN ?= aurora-efficiency
MODE ?= attack
BRIEF ?= agents

LOGS ?= logs
FILE_SIZE_BASE ?= origin/main

.PHONY: run view explore read cost help gates images setup public-repo fixture-run

help:
	@echo "make setup                                # check toolchain, sync deps, validate config"
	@echo "make public-repo                          # build the curated public tree into dist/public-repo (verified)"
	@echo "make run RUN=aurora-efficiency MODE=attack|honest  # live-model episode in a stack, write the audit bundle"
	@echo "make run RUN=aurora-efficiency.fast               # the smoke run: cheapest models, small budgets"
	@echo "make view RUN=aurora-efficiency                       # open the latest report.html + inspect view"
	@echo "make explore [LOGS=logs]                  # build the run explorer over every run in LOGS and open it"
	@echo "make read RUN_DIR=<run directory> [BRIEF=agents|monitors]  # an agent reads one run and reports"
	@echo "make cost RUN_DIR=<run directory> [OTHER=<run directory>]  # what a run cost, or how a second run's cost differs"
	@echo "make fixture-run                          # play the smoke run and store it as the tests' run fixture"
	@echo "make gates                                # file size, changelog, ruff + ruff format + ty + pytest"
	@echo "make images                               # build this checkout's sandbox, runner, worker and engine images, which the Docker tests need"
	@echo "  (a run needs Docker Engine 28 or later)"

setup:
	@command -v uv >/dev/null || (echo "uv not found: install from https://docs.astral.sh/uv/" && exit 1)
	@uv sync
	@uv run python -c "from loc_arena.config import load_run_config as L; from loc_arena.monitors.registry import load_monitor_specs as S, firewall_violations as F; L('configs/aurora-efficiency.yaml'); L('configs/aurora-efficiency.fast.yaml'); v=F(S('configs/monitors.yaml')); assert not v, v; print('config OK: aurora-efficiency, its smoke run and monitors.yaml (firewall clean)')"
	@(command -v docker >/dev/null && docker info >/dev/null 2>&1 && echo "docker: available") || echo "docker: not found (a run needs it; most tests run without it)"
	@echo "setup complete. Set OPENROUTER_API_KEY in .env, then:  make run"

run:
	@uv run python -m loc_arena.cli run --run $(RUN) --mode $(MODE)

view:
	@uv run python -m loc_arena.cli view --run $(RUN)

explore:
	@uv run python -m loc_arena.cli explore $(LOGS)

read:
	@uv run python -m loc_arena.cli read "$(RUN_DIR)" --brief "$(BRIEF)" --run "$(RUN)"

cost:
	@uv run python -m loc_arena.cli cost "$(RUN_DIR)" $(OTHER)

fixture-run:
	@uv run python scripts/record_run_fixture.py

public-repo:
	@uv run python scripts/make_public.py

# The sandbox, runner, worker and engine images under this checkout's own names (the stack settings' defaults):
# a Docker test skips without the one it checks for, and some start the runners' or the nodes' without building
# it. A run builds its own.
STACK_IMAGES = $(shell uv run python -c "from loc_arena.settings import StackSettings as S; s = S(); print(s.sandbox_image, s.runner_image, s.worker_image, s.image)")

images:
	@set -- $(STACK_IMAGES); for target in sandbox runner worker engine; do \
		docker build $${SSL_CERT_FILE:+--secret "id=ca_bundle,src=$$SSL_CERT_FILE"} --target $$target -t $$1 . || exit 1; shift; done

gates:
	@python3 scripts/check_file_size.py --base "$(FILE_SIZE_BASE)"
	@python3 scripts/check_changelog.py $(if $(CHANGELOG_PULL_REQUEST),--pull-request $(CHANGELOG_PULL_REQUEST))
	@uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pytest -q -n auto --dist loadscope
