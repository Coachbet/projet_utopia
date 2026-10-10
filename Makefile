.DEFAULT_GOAL := help
SHELL := /bin/bash

help: ## Liste les cibles
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Crée le venv et installe tout (sauf torch)
	uv sync --group dev

install-voice: ## Ajoute torch + transformers (long, ~2 Go)
	uv sync --group dev --extra voice

run: ## Lance l'API FastAPI en rechargement auto
	uv run uvicorn vocalguard.interface.app:create_app --factory --reload --host 0.0.0.0 --port 8000

tunnel: ## Expose le port 8000 via ngrok (autre terminal)
	ngrok http 8000

check-aws: ## H0 — vérifie Bedrock et Transcribe avec les creds courants
	uv run python scripts/check_aws_permissions.py

replay: ## Rejoue un scénario dans le pipeline, sans téléphone (SCENARIO=...)
	uv run python scripts/replay_scenario.py $(SCENARIO)

test: ## Tests unitaires
	uv run pytest -q

lint: ## Lint + format
	uv run ruff check --fix src tests scripts
	uv run ruff format src tests scripts

clean: ## Supprime les caches
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache

.PHONY: help install install-voice run tunnel check-aws replay test lint clean
