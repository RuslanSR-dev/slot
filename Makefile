# One source of truth: CI runs exactly these targets, so any red build
# can be reproduced locally with the same command.

BOOKING := services/booking
# Empty without git (e.g. before the first commit): the service then reports "unknown".
export SLOT_BUILD_SHA ?= $(shell git rev-parse --short HEAD 2>/dev/null)

# Testcontainers talks to Docker through its API. With Colima the socket is not
# at the default path, so take it from the active docker context (ADR-0006).
export DOCKER_HOST ?= $(shell docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null)
# Path of the socket inside the Docker VM, mounted into the cleanup container.
export TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE ?= /var/run/docker.sock

.PHONY: help install format lint typecheck test-unit test-mutation test-component check up smoke down logs

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-12s %s\n", $$1, $$2}'

install: ## Install dependencies from the lock file
	cd $(BOOKING) && uv sync --locked

format: ## Auto-format the code
	cd $(BOOKING) && uv run ruff format . && uv run ruff check --fix .

lint: ## Gate: formatting and lint rules
	cd $(BOOKING) && uv run ruff format --check . && uv run ruff check .

typecheck: ## Gate: strict typing
	cd $(BOOKING) && uv run mypy src tests tools

test-unit: ## Gate: unit tests (fast, no Docker)
	cd $(BOOKING) && uv run pytest tests/unit --junitxml=reports/junit-unit.xml

test-mutation: ## Gate: mutation score of the domain rules (do the tests notice broken logic?)
	cd $(BOOKING) && rm -rf mutants && mkdir -p reports && \
		(uv run mutmut run > reports/mutmut-run.log 2>&1 || (tail -20 reports/mutmut-run.log; exit 1)) && \
		uv run mutmut export-cicd-stats > /dev/null && uv run python tools/mutation_gate.py

test-component: ## Gate: unit + component tests on real Postgres, with coverage threshold
	cd $(BOOKING) && uv run pytest tests/unit tests/component --cov --cov-report=term-missing \
		--junitxml=reports/junit-component.xml

check: lint typecheck test-unit test-mutation ## Everything a PR must pass before the stack is built

up: ## Build images and start the whole stack, wait until healthy
	docker compose up --detach --build --wait

smoke: ## Gate: smoke tests against the running stack
	cd $(BOOKING) && SLOT_EXPECTED_BUILD_SHA=$(SLOT_BUILD_SHA) \
		uv run pytest tests/smoke --junitxml=reports/junit-smoke.xml

logs: ## Show stack logs
	docker compose logs --no-color

down: ## Stop the stack and remove its data
	docker compose down --volumes --remove-orphans
