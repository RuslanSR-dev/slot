# One source of truth: CI runs exactly these targets, so any red build
# can be reproduced locally with the same command.
#
# Service targets run for every service; narrow them with SERVICES, e.g.
#   make test-component SERVICES=payments

SERVICES ?= booking payments notifier
SERVICE_DIRS = $(addprefix services/,$(SERVICES))
# Every Python project in the repo: services plus the system smoke tests.
PROJECTS = $(SERVICE_DIRS) smoke

export SLOT_BUILD_SHA ?= $(shell git rev-parse --short HEAD 2>/dev/null)

# Several stacks can run on one machine at the same time - a worktree, a second
# branch, a parallel agent. Everything that would collide is derived from two
# variables, so one offset is enough:
#   make up smoke down SLOT_PORT_OFFSET=20 COMPOSE_PROJECT_NAME=slot-experiment
export COMPOSE_PROJECT_NAME ?= slot
SLOT_PORT_OFFSET ?= 0
export SLOT_BOOKING_PORT ?= $(shell expr 8000 + $(SLOT_PORT_OFFSET))
export SLOT_PAYMENTS_PORT ?= $(shell expr 8001 + $(SLOT_PORT_OFFSET))
export SLOT_NOTIFIER_PORT ?= $(shell expr 8002 + $(SLOT_PORT_OFFSET))
export SLOT_NOTIFYGW_PORT ?= $(shell expr 8090 + $(SLOT_PORT_OFFSET))
# The smoke tests know only URLs, so they follow the ports automatically.
export SLOT_BOOKING_URL ?= http://127.0.0.1:$(SLOT_BOOKING_PORT)
export SLOT_PAYMENTS_URL ?= http://127.0.0.1:$(SLOT_PAYMENTS_PORT)
export SLOT_NOTIFIER_URL ?= http://127.0.0.1:$(SLOT_NOTIFIER_PORT)

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
# Secret scanner, pinned by digest for the same reason as oasdiff: a gate must
# not change its mind because a tag moved.
GITLEAKS = zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f
# Inside the repo, so Docker (also through Colima) can mount it.
TMP = .tmp
# Reports of the repository-wide gates. Ignored by git, uploaded by CI.
REPORTS = reports
PACTS = contracts/pacts
# The published shape of the booking event (ADR-0010).
EVENTS = contracts/events/booking.v1.json
# Contract files the tests write themselves. They must be committed as they
# are generated, so a changed contract is seen in review.
GENERATED_CONTRACTS = $(PACTS) contracts/events/consumers
# Repository tools reuse the dev tools of the smoke project: no extra project to maintain.
TOOLS_RUN = uv run --project smoke --quiet

# Run a command in each directory; stop at the first failure and say where.
in_each = for dir in $(1); do echo "--- $$dir"; (cd $$dir && $(2)) || exit 1; done

.PHONY: help install format lint typecheck test-unit test-mutation test-component check \
	openapi openapi-check openapi-breaking events events-check events-breaking \
	test-contract base-pacts test-tools fitness changed-services up smoke down logs \
	security audit secrets

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
	@if git status --porcelain -- $(GENERATED_CONTRACTS) | grep .; then \
		echo "contract files changed: review the new contract and commit it"; exit 1; fi

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

fitness: ## Gate: fitness function - no service imports the code of another (ADR-0002)
	@$(TOOLS_RUN) python tools/import_boundaries.py

security: audit secrets ## Both security gates: dependencies and secrets (ADR-0018)

audit: ## Gate: known vulnerabilities in the dependencies of every project (ADR-0018)
	@$(TOOLS_RUN) python tools/accepted_risks.py
	@$(TOOLS_RUN) python tools/dependency_audit.py $(PROJECTS)

# Two scans, because they answer different questions. The working tree says
# "is a secret about to be committed", the history says "has one ever been" -
# and a secret that was committed and then deleted is still published.
# The history is scanned in a throwaway bare repository holding exactly what
# HEAD reaches, built by pushing HEAD. A separate repository is needed at all
# because in a worktree `.git` is a file pointing outside the directory Docker
# can mount; pushing HEAD rather than cloning says which commits are meant
# without depending on how clone picks a branch, and HEAD is a commit in CI
# too, where a pull request is checked out detached.
# What HEAD reaches and nothing else: the gate answers for this change, every
# other branch is scanned by its own pull request, and with a shared object
# store "everything" would drag half a dozen unfinished branches into it.
secrets: ## Gate: no secret in the working tree or anywhere in the history (ADR-0018)
	@mkdir -p $(REPORTS) $(TMP)
	@rm -rf $(TMP)/history.git
	@git init --bare --quiet $(TMP)/history.git
	@git push --quiet $(TMP)/history.git HEAD:refs/heads/scanned
	@git --git-dir=$(TMP)/history.git symbolic-ref HEAD refs/heads/scanned
	@docker run --rm -v "$(CURDIR):/repo:ro" -v "$(CURDIR)/$(REPORTS):/out" $(GITLEAKS) \
		dir /repo --config /repo/security/gitleaks.toml --no-banner --redact=50 --verbose \
		--report-format json --report-path /out/secrets-worktree.json
	@docker run --rm -v "$(CURDIR)/$(TMP)/history.git:/history:ro" \
		-v "$(CURDIR)/security:/config:ro" -v "$(CURDIR)/$(REPORTS):/out" $(GITLEAKS) \
		git /history --config /config/gitleaks.toml --no-banner --redact=50 --verbose \
		--report-format json --report-path /out/secrets-history.json

changed-services: ## Print the services the changes since BASE_REF can break (JSON)
	@$(TOOLS_RUN) python tools/changed_services.py $(BASE_REF)

check: lint typecheck test-tools fitness test-unit test-contract test-mutation openapi-check events-check ## Everything a PR must pass before Docker is needed

up: ## Build images and start the whole stack, wait until healthy
	docker compose up --detach --build --wait

smoke: ## Gate: smoke tests of the whole running stack
	cd smoke && SLOT_EXPECTED_BUILD_SHA=$(SLOT_BUILD_SHA) \
		uv run pytest tests --junitxml=reports/junit-smoke.xml

logs: ## Show stack logs
	docker compose logs --no-color

down: ## Stop the stack and remove its data
	docker compose down --volumes --remove-orphans
