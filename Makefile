.DEFAULT_GOAL := help
SHELL := /bin/bash

help: ## Liste les cibles
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Crée le venv et installe tout (sauf torch)
	uv sync --group dev

install-voice: ## Ajoute torch + transformers (long, ~2 Go)
	uv sync --group dev --extra voice

PORT ?= 8000
# Surchargeable : `make run PORT=8001`. Indispensable quand 8000 est déjà pris
# (un conteneur Docker, un autre uvicorn) — sinon le serveur démarre « quelque
# part » et Twilio parle à la mauvaise application, sans que rien ne le dise.
# `make tunnel` doit TOUJOURS recevoir le même port que `make run`.

run: ## Lance l'API FastAPI en rechargement auto (PORT=8001 pour changer de port)
	uv run uvicorn vocalguard.interface.app:create_app --factory --reload --host 0.0.0.0 --port $(PORT)

tunnel: ## Expose le port via ngrok, autre terminal (PORT=8001 pour changer)
	ngrok http $(PORT)

check-aws: ## H0 — vérifie Bedrock et Transcribe avec les creds courants
	uv run python scripts/check_aws_permissions.py

replay: ## Rejoue un scénario dans le pipeline, sans téléphone (SCENARIO=...)
	uv run python scripts/replay_scenario.py $(SCENARIO)

EVAL_MODE ?= offline
EVAL_OUTPUT ?= artifacts/evaluation/$(EVAL_MODE)
evaluate: ## Évalue les scénarios (EVAL_MODE=offline|keywords|bedrock)
	uv run python scripts/evaluate_scenarios.py --mode $(EVAL_MODE) --output-dir $(EVAL_OUTPUT)

calibrate-voice: ## Calibre la voix synthétique (HUMAN="a.wav b.wav" FAKE="c.wav d.wav")
	uv run python scripts/calibrate_voice.py --human $(HUMAN) --fake $(FAKE)

test: ## Tests unitaires
	uv run pytest -q

lint: ## Lint + format
	uv run ruff check --fix src tests scripts
	uv run ruff format src tests scripts

test-ui: ## QA mobile et captures (Playwright installé, voir docs/RECETTE_MVP.md)
	node scripts/check_mobile_ui.cjs
	node scripts/check_mobile_races.cjs

clean: ## Supprime les caches
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache

.PHONY: help install install-voice run tunnel check-aws replay evaluate test lint test-ui clean
