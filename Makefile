.PHONY: install dev-install run worker seed-admin migrate migrate-new test lint format clean docker-up docker-down docker-logs

# ---- Python Environment ----
install:
	uv pip install -e .

dev-install:
	uv pip install -e ".[dev]"

# ---- Run Services ----
run:
	uvicorn etl_agent.main:app --reload --host 0.0.0.0 --port 8000

worker:
	celery -A etl_agent.workers.celery_app worker --loglevel=info

# ---- Data ---- 
seed-admin:
	python -m etl_agent.cli

# ---- Database ----
migrate:
	alembic upgrade head

migrate-new:
	alembic revision --autogenerate -m "$(MSG)"

# ---- Testing & Linting ----
test:
	pytest -v

lint:
	ruff check src/ tests/

format:
	ruff format src/ tests/

# ---- Docker ----
docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

# ---- Cleanup ----
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; \
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null; \
	rm -rf .ruff_cache .mypy_cache 2>/dev/null; \
	echo "Cleaned"
