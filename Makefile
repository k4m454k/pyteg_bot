.PHONY: build_test test test_api lint

PYTEGBOT_TEST_DOCKER_BASE_URL ?= $(shell docker context inspect --format '{{(index .Endpoints "docker").Host}}' 2>/dev/null)

build_test:
	docker compose build runner-base
	cd api && poetry install --no-interaction --no-ansi
	cd api && poetry run pip install --no-deps --editable ../shared

test: test_api

test_api:
	cd api && PYTHONPATH="../shared/src:src" PYTEGBOT_TEST_DOCKER_BASE_URL="$(PYTEGBOT_TEST_DOCKER_BASE_URL)" poetry run pytest -q --tb=short

lint:
	cd shared && poetry check
	cd api && poetry check
	cd bot && poetry check
	python3 -m compileall api/src bot/src shared/src runner/executor.py
