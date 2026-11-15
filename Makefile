.DEFAULT_GOAL := help
UV ?= uv
DEV_IMAGE ?= opsmith-dev

.PHONY: help setup lint fmt type test cov check demo run docker-dev docker-test clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

setup: ## Install all dependencies (incl. dev + extras)
	$(UV) sync --all-extras

lint: ## Ruff lint + format check
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

fmt: ## Auto-fix lint and format
	$(UV) run ruff check src tests --fix
	$(UV) run ruff format src tests

type: ## mypy
	$(UV) run mypy

test: ## Run tests
	$(UV) run pytest -q

cov: ## Tests with coverage report
	$(UV) run pytest -q --cov --cov-report=term

check: lint type cov ## Everything CI runs

demo: ## Offline end-to-end demo
	$(UV) run opsmith demo --dir ./opsmith-demo

run: ## API + scheduler + worker (development)
	$(UV) run opsmith run

docker-dev: ## Build the dev image
	docker build --target dev -t $(DEV_IMAGE) .

docker-test: docker-dev ## Run lint, types and tests inside the dev image
	docker run --rm $(DEV_IMAGE) sh -c "ruff check src tests && ruff format --check src tests && mypy && pytest -q --cov"

clean: ## Remove local artifacts
	rm -rf data opsmith-demo .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
