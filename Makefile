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

# Breaking API changes are checked against this ref (ADR-0007). CI sets it to the
# PR base or the previous commit of main.
BASE_REF ?= origin/main
# Pinned by digest: the gate must not change behaviour because "latest" moved.
OASDIFF = tufin/oasdiff@sha256:0286f138545a39010525df6c1bea67ffafacb384ef800effffa63bbd04718ce5
# Inside the repo, so Docker (also through Colima) can mount it.
TMP = .tmp
PACTS = contracts/pacts
# The published shape of the booking event (ADR-0010).
EVENTS = contracts/events/booking.v1.json
# Repository tools reuse the dev tools of the smoke project: no extra project to maintain.
TOOLS_RUN = uv run --project smoke --quiet

# Run a command in each directory; stop at the first failure and say where.
in_each = for dir in $(1); do echo "--- $$dir"; (cd $$dir && $(2)) || exit 1; done

.PHONY: help install format lint typecheck test-unit test-mutation test-component check \
	openapi openapi-check openapi-breaking events events-check events-breaking \
	test-contract base-pacts test-tools changed-services up smoke down logs

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-15s %s\n", $$1, $$2}'

install: ## Install dependencies from the lock files
	@$(call in_each,$(PROJECTS),uv sync --locked)

format: ## Auto-format the code
	@$(call in_each,$(PROJECTS),uv run ruff format . && uv run ruff check --fix .)
	@echo "--- tools" && $(TOOLS_RUN) ruff format tools && $(TOOLS_RUN) ruff check --fix tools

lint: ## Gate: formatting and lint rules
	@$(call in_each,$(PROJECTS),uv run ruff format --check . && uv run ruff check .)
	@echo "--- tools" && $(TOOLS_RUN) ruff format --check tools && $(TOOLS_RUN) ruff check tools

typecheck: ## Gate: strict typing
	@$(call in_each,$(PROJECTS),uv run mypy)
	@echo "--- tools" && $(TOOLS_RUN) mypy --strict tools

test-unit: ## Gate: unit tests (fast, no Docker)
	@$(call in_each,$(SERVICE_DIRS),uv run pytest tests/unit --junitxml=reports/junit-unit.xml)

test-mutation: ## Gate: mutation score of the domain rules (do the tests notice broken logic?)
	@$(call in_each,$(SERVICE_DIRS),rm -rf mutants && mkdir -p reports && \
		(uv run mutmut run > reports/mutmut-run.log 2>&1 || \
			(tail -20 reports/mutmut-run.log; exit 1)) && \
		uv run mutmut export-cicd-stats > /dev/null && uv run python tools/mutation_gate.py)

test-contract: ## Gate: consumer contract tests; the pact files they write must be committed
	@$(call in_each,$(SERVICE_DIRS),uv run pytest tests/contract --junitxml=reports/junit-contract.xml)
	@if git status --porcelain -- $(PACTS) | grep .; then \
		echo "pact files changed: review the new contract and commit it"; exit 1; fi

base-pacts: ## Copy the pact files of BASE_REF: providers are verified against them too
	@rm -rf $(TMP)/pacts-base && mkdir -p $(TMP)/pacts-base
	@for file in $$(git ls-tree --name-only $(BASE_REF) $(PACTS)/ 2>/dev/null); do \
		git show $(BASE_REF):$$file > $(TMP)/pacts-base/$$(basename $$file); done
	@echo "pact files from $(BASE_REF): $$(ls $(TMP)/pacts-base | wc -l | tr -d ' ')"

# Providers verify the pacts of the branch and of BASE_REF (ADR-0007).
test-component: export SLOT_PACT_DIRS = $(CURDIR)/$(PACTS):$(CURDIR)/$(TMP)/pacts-base
test-component: base-pacts ## Gate: unit + component tests on real Postgres, with coverage threshold
	@$(call in_each,$(SERVICE_DIRS),uv run pytest tests/unit tests/component \
		--cov --cov-report=term-missing --junitxml=reports/junit-component.xml)

openapi: ## Regenerate the committed OpenAPI schemas from code
	@$(call in_each,$(SERVICE_DIRS),uv run python tools/export_openapi.py > openapi.json)

openapi-check: ## Gate: committed OpenAPI schemas match the code
	@$(call in_each,$(SERVICE_DIRS),uv run python tools/export_openapi.py | \
		diff -u openapi.json - > /dev/null || \
		(echo "openapi.json is stale: run make openapi and commit it"; exit 1))

openapi-breaking: ## Gate: no breaking API changes against BASE_REF (oasdiff)
	@mkdir -p $(TMP)/base
	@for service in $(SERVICES); do \
		spec=services/$$service/openapi.json; \
		if ! git cat-file -e $(BASE_REF):$$spec 2>/dev/null; then \
			echo "--- $$service: no schema in $(BASE_REF), nothing to break"; continue; fi; \
		git show $(BASE_REF):$$spec > $(TMP)/base/$$service.json; \
		echo "--- $$service: $(BASE_REF) -> working tree"; \
		docker run --rm -v "$(CURDIR):/repo:ro" -w /repo $(OASDIFF) \
			breaking $(TMP)/base/$$service.json $$spec --fail-on ERR || exit 1; \
	done

events: ## Regenerate the committed event schema from code
	@cd services/booking && uv run python tools/export_events.py > $(CURDIR)/$(EVENTS)

events-check: ## Gate: the committed event schema matches the code
	@cd services/booking && uv run python tools/export_events.py | \
		diff -u $(CURDIR)/$(EVENTS) - > /dev/null || \
		(echo "$(EVENTS) is stale: run make events and commit it"; exit 1)

events-breaking: ## Gate: no breaking changes of the event schema against BASE_REF
	@mkdir -p $(TMP)/base
	@git show $(BASE_REF):$(EVENTS) > $(TMP)/base/events.json 2>/dev/null || \
		rm -f $(TMP)/base/events.json
	@$(TOOLS_RUN) python tools/event_schema_diff.py $(TMP)/base/events.json $(EVENTS)

test-tools: ## Gate: tests of the repository tools (test impact analysis)
	@$(TOOLS_RUN) pytest tools -q

changed-services: ## Print the services the changes since BASE_REF can break (JSON)
	@$(TOOLS_RUN) python tools/changed_services.py $(BASE_REF)

check: lint typecheck test-tools test-unit test-contract test-mutation openapi-check events-check ## Everything a PR must pass before Docker is needed

up: ## Build images and start the whole stack, wait until healthy
	docker compose up --detach --build --wait

smoke: ## Gate: smoke tests of the whole running stack
	cd smoke && SLOT_EXPECTED_BUILD_SHA=$(SLOT_BUILD_SHA) \
		uv run pytest tests --junitxml=reports/junit-smoke.xml

logs: ## Show stack logs
	docker compose logs --no-color

down: ## Stop the stack and remove its data
	docker compose down --volumes --remove-orphans
