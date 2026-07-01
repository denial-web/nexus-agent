.PHONY: install dev test test-fast test-cov lint format typecheck audit migrate serve docker-up docker-down clean injection-demo-gif

install:
	pip install -r requirements.txt

dev: install migrate
	uvicorn app.main:app --reload --port 9000

test:
	pytest tests/ -v --tb=short

test-fast:
	pytest tests/ -x -q --tb=line

test-cov:
	pytest tests/ -v --tb=short --cov=app --cov-report=term-missing --cov-report=html:htmlcov

lint:
	ruff check app/ tests/

format:
	ruff format app/ tests/
	ruff check --fix app/ tests/

typecheck:
	mypy app/ --ignore-missing-imports

audit:
	pip-audit -r requirements.txt

migrate:
	alembic upgrade head

migration:
	@read -p "Migration message: " msg && alembic revision --autogenerate -m "$$msg"

serve:
	uvicorn app.main:app --host 0.0.0.0 --port 9000

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -f test_nexus.db nexus.db
	rm -rf .pytest_cache .mypy_cache

injection-demo-gif:
	python scripts/build_injection_demo_gif.py
