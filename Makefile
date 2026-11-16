.DEFAULT_GOAL := help
UV ?= uv
DEV_IMAGE ?= opsmith-dev

.PHONY: help setup lint fmt type test cov docs check demo run docker-dev docker-test clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

setup: ## Install all dependencies (incl. dev + extras)
	$(UV) sync --all-extras

lint: ## Ruff lint + format check
	$(UV) run ruff check src tests scripts
	$(UV) run ruff format --check src tests scripts

fmt: ## Auto-fix lint and format
	$(UV) run ruff check src tests scripts --fix
	$(UV) run ruff format src tests scripts

type: ## mypy
	$(UV) run mypy

test: ## Run tests
	$(UV) run pytest -q

cov: ## Tests with coverage report
	$(UV) run pytest -q --cov --cov-report=term

docs: ## Check documentation links, anchors and cited tests
	$(UV) run python scripts/check_docs.py

check: lint type cov docs ## Everything CI runs

demo: ## Offline end-to-end demo
	$(UV) run opsmith demo --dir ./opsmith-demo

run: ## API + scheduler + worker (development)
	$(UV) run opsmith run

docker-dev: ## Build the dev image
	docker build --target dev -t $(DEV_IMAGE) .

docker-test: docker-dev ## Run lint, types and tests inside the dev image
	docker run --rm $(DEV_IMAGE) sh -c "ruff check src tests scripts && ruff format --check src tests scripts && mypy && pytest -q --cov && python scripts/check_docs.py"

clean: ## Remove local artifacts
	rm -rf data opsmith-demo .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
