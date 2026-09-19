# One source of truth: CI runs exactly these targets, so any red build
# can be reproduced locally with the same command.
#
# Service targets run for every service; narrow them with SERVICES, e.g.
#   make test-component SERVICES=payments

SERVICES ?= booking payments
SERVICE_DIRS = $(addprefix services/,$(SERVICES))
# Every Python project in the repo: services plus the system smoke tests.
PROJECTS = $(SERVICE_DIRS) smoke

export SLOT_BUILD_SHA ?= $(shell git rev-parse --short HEAD 2>/dev/null)

# Testcontainers talks to Docker through its API. With Colima the socket is not
# at the default path, so take it from the active docker context (ADR-0006).
export DOCKER_HOST ?= $(shell docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null)
# Path of the socket inside the Docker VM, mounted into the cleanup container.
export TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE ?= /var/run/docker.sock

# Run a command in each directory; stop at the first failure and say where.
in_each = for dir in $(1); do echo "--- $$dir"; (cd $$dir && $(2)) || exit 1; done

.PHONY: help install format lint typecheck test-unit test-mutation test-component check \
	up smoke down logs

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-15s %s\n", $$1, $$2}'

install: ## Install dependencies from the lock files
	@$(call in_each,$(PROJECTS),uv sync --locked)

format: ## Auto-format the code
	@$(call in_each,$(PROJECTS),uv run ruff format . && uv run ruff check --fix .)

lint: ## Gate: formatting and lint rules
	@$(call in_each,$(PROJECTS),uv run ruff format --check . && uv run ruff check .)

typecheck: ## Gate: strict typing
	@$(call in_each,$(PROJECTS),uv run mypy)

test-unit: ## Gate: unit tests (fast, no Docker)
	@$(call in_each,$(SERVICE_DIRS),uv run pytest tests/unit --junitxml=reports/junit-unit.xml)

test-mutation: ## Gate: mutation score of the domain rules (do the tests notice broken logic?)
	@$(call in_each,$(SERVICE_DIRS),rm -rf mutants && mkdir -p reports && \
		(uv run mutmut run > reports/mutmut-run.log 2>&1 || \
			(tail -20 reports/mutmut-run.log; exit 1)) && \
		uv run mutmut export-cicd-stats > /dev/null && uv run python tools/mutation_gate.py)

test-component: ## Gate: unit + component tests on real Postgres, with coverage threshold
	@$(call in_each,$(SERVICE_DIRS),uv run pytest tests/unit tests/component \
		--cov --cov-report=term-missing --junitxml=reports/junit-component.xml)

check: lint typecheck test-unit test-mutation ## Everything a PR must pass before Docker is needed

up: ## Build images and start the whole stack, wait until healthy
	docker compose up --detach --build --wait

smoke: ## Gate: smoke tests of the whole running stack
	cd smoke && SLOT_EXPECTED_BUILD_SHA=$(SLOT_BUILD_SHA) \
		uv run pytest tests --junitxml=reports/junit-smoke.xml

logs: ## Show stack logs
	docker compose logs --no-color

down: ## Stop the stack and remove its data
	docker compose down --volumes --remove-orphans
