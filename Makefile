.PHONY: up down install migrate revision test lint fmt dev worker

up:
	docker compose up -d

down:
	docker compose down

install:
	python -m pip install -e ".[dev]"

migrate:
	alembic upgrade head

revision:
	alembic revision --autogenerate -m "$(m)"

test:
	pytest

lint:
	ruff check app tests scripts
	mypy app

fmt:
	ruff check --fix app tests scripts
	ruff format app tests scripts

dev:
	uvicorn app.main:app --reload --port 8000

worker:
	arq app.workers.settings.WorkerSettings
