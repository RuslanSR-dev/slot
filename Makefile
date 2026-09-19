# One source of truth: CI runs exactly these targets, so any red build
# can be reproduced locally with the same command.

BOOKING := services/booking
# Empty without git (e.g. before the first commit): the service then reports "unknown".
export SLOT_BUILD_SHA ?= $(shell git rev-parse --short HEAD 2>/dev/null)

.PHONY: help install format lint typecheck test-unit check up smoke down logs

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-12s %s\n", $$1, $$2}'

install: ## Install dependencies from the lock file
	cd $(BOOKING) && uv sync --locked

format: ## Auto-format the code
	cd $(BOOKING) && uv run ruff format . && uv run ruff check --fix .

lint: ## Gate: formatting and lint rules
	cd $(BOOKING) && uv run ruff format --check . && uv run ruff check .

typecheck: ## Gate: strict typing
	cd $(BOOKING) && uv run mypy src tests

test-unit: ## Gate: unit tests with coverage threshold
	cd $(BOOKING) && uv run pytest tests/unit --cov --cov-report=term-missing \
		--junitxml=reports/junit-unit.xml

check: lint typecheck test-unit ## Everything a PR must pass before the stack is built

up: ## Build images and start the whole stack, wait until healthy
	docker compose up --detach --build --wait

smoke: ## Gate: smoke tests against the running stack
	cd $(BOOKING) && SLOT_EXPECTED_BUILD_SHA=$(SLOT_BUILD_SHA) \
		uv run pytest tests/smoke --junitxml=reports/junit-smoke.xml

logs: ## Show stack logs
	docker compose logs --no-color

down: ## Stop the stack and remove its data
	docker compose down --volumes --remove-orphans
