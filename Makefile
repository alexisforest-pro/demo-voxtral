.PHONY: installdeps start-worker start-app lint format typecheck lint-workflows test test-browser test-all check check-all docker-build docker-run

IMAGE ?= voxbridge-worker

## Install the locked runtime and development dependencies
installdeps:
	uv sync --locked

## Register the five application workflows and start polling
start-worker:
	uv run python -m workers.worker

## Serve the frontend and backend from a single localhost origin
start-app:
	uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000

## Lint and check Python formatting
lint:
	uv run ruff check src/backend/ src/workers/
	uv run ruff format --check src/backend/ src/workers/

## Format Python and apply Ruff autofixes
format:
	uv run ruff format src/backend/ src/workers/
	uv run ruff check --fix src/backend/ src/workers/

## Type-check the registered workflow definitions
typecheck:
	uv run mypy src/workers/workflows/

## Check workflows for determinism, I/O, and structure issues
lint-workflows:
	uv run semgrep --config .agents/skills/workflows/scripts/linting/rules/ --quiet src/workers/workflows/

## Fast offline API, workflow, storage, and streaming regressions
test:
	uv run pytest src/backend/tests/ -q --ignore-glob='*_browser.py'

## Browser UI, microphone, and playback-lifecycle regressions
test-browser:
	uv run pytest src/backend/tests/test_*_browser.py -q

## Every offline regression, including browsers
test-all:
	uv run pytest src/backend/tests/ -q

## Fast development checks; browsers are available separately
check: lint typecheck lint-workflows test

## Full checks, including browser regressions
check-all: lint typecheck lint-workflows test-all

## Build the worker-only container
docker-build:
	docker build -t $(IMAGE) .

## Run the worker with the same data directory used by the host backend
docker-run:
	docker run --rm --env-file .env -e DATA_DIR=/app/data -v "$(CURDIR)/data:/app/data" $(IMAGE)
