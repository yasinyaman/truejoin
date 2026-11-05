# ===========================================
# Warp Engine - Makefile
# ===========================================

.PHONY: help install lock dev test test-integration lint format clean docker-build docker-up docker-up-mssql docker-down docker-logs docker-shell docker-restart docker-clean db-reset db-shell-pg db-shell-mysql db-shell-mssql ssl-certs quickstart

# Load local secrets/credentials from .env when present (copy .env.example -> .env).
-include .env
export

# Default target
help:
	@echo "Warp Engine - Available Commands"
	@echo "============================="
	@echo ""
	@echo "Development:"
	@echo "  make install      - Install dev + llm + odbc extras (editable)"
	@echo "  make lock         - Regenerate the hashed lock files (requires uv)"
	@echo "  make dev          - Run development server"
	@echo "  make test         - Run the unit test suite"
	@echo "  make test-integration - Run DB integration tests (needs Docker)"
	@echo "  make lint         - Lint, format-check, type-check, import contracts"
	@echo "  make format       - Auto-fix lint + format code (ruff)"
	@echo ""
	@echo "Docker (Development):"
	@echo "  make docker-build - Build Docker images"
	@echo "  make docker-up    - Start all services"
	@echo "  make docker-up-mssql - Also start SQL Server (profile mssql, needs MSSQL_PASS)"
	@echo "  make docker-down  - Stop all services"
	@echo "  make docker-logs  - View logs"
	@echo "  make docker-shell - Shell into API container"
	@echo ""
	@echo "Utilities:"
	@echo "  make clean        - Clean up generated files"
	@echo "  make ssl-certs    - Generate self-signed SSL certs"

# ===========================================
# Development
# ===========================================

install:
	uv sync --all-extras

lock:
	uv lock

dev:
	uv run trio serve

test:
	uv run pytest tests/ -v --cov=trio --cov-report=html

# Real PostgreSQL + MySQL via testcontainers (Docker must be running). SQL
# Server runs too when the Microsoft ODBC driver is installed and the Docker
# engine is x86-64 (CI); otherwise it is skipped with a reason.
test-integration:
	uv run pytest tests/integration -m integration -v

lint:
	uv run ruff check trio tests bench demo
	uv run ruff format --check trio tests bench demo
	uv run mypy trio

format:
	uv run ruff check --fix trio tests bench demo
	uv run ruff format trio tests bench demo

# ===========================================
# Docker Development
# ===========================================

docker-build:
	docker-compose build

docker-up:
	docker-compose up -d
	@echo ""
	@echo "Services started!"
	@echo "  API:     http://localhost:8000"
	@echo "  Swagger: http://localhost:8000/docs"
	@echo "  Adminer: http://localhost:8080"
	@echo ""

docker-up-mssql:
	docker-compose --profile mssql up -d

docker-down:
	docker-compose --profile mssql down

docker-logs:
	docker-compose logs -f

docker-shell:
	docker-compose exec api /bin/bash

docker-restart:
	docker-compose restart api

docker-clean:
	docker-compose down -v --rmi local

# ===========================================
# Database
# ===========================================

db-reset:
	docker-compose down -v
	docker-compose up -d postgres mysql
	@echo "Waiting for databases to initialize..."
	@sleep 10
	docker-compose up -d api

db-shell-pg:
	docker-compose exec postgres psql -U postgres -d testdb

db-shell-mysql:
	docker-compose exec mysql mysql -u root -p"$(MYSQL_PASS)" $(MYSQL_DB)

db-shell-mssql:
	docker-compose exec mssql /opt/mssql-tools18/bin/sqlcmd -C -U sa -P "$(MSSQL_PASS)"

# ===========================================
# Utilities
# ===========================================

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "htmlcov" -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name ".coverage" -delete 2>/dev/null || true

ssl-certs:
	@mkdir -p docker/nginx/ssl
	openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
		-keyout docker/nginx/ssl/key.pem \
		-out docker/nginx/ssl/cert.pem \
		-subj "/C=US/ST=State/L=City/O=Organization/CN=localhost"
	@echo "SSL certificates generated in docker/nginx/ssl/"

# ===========================================
# Quick Start
# ===========================================

quickstart: docker-build docker-up
	@echo ""
	@echo "Quick start complete! Try these commands:"
	@echo ""
	@echo "  # List all users"
	@echo "  curl http://localhost:8000/api/v1/users"
	@echo ""
	@echo "  # Get products with filtering"
	@echo "  curl 'http://localhost:8000/api/v1/products?filter[status]=active'"
	@echo ""
	@echo "  # Create a new user"
	@echo "  curl -X POST http://localhost:8000/api/v1/users \\"
	@echo "    -H 'Content-Type: application/json' \\"
	@echo "    -d '{\"username\":\"test\",\"email\":\"test@example.com\",\"password_hash\":\"hash\"}'"
	@echo ""
