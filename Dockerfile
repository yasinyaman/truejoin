# syntax=docker/dockerfile:1
#
# One image for the whole product. Stage 1 resolves the locked dependency set
# with uv; stage 2 is a slim runtime with the ODBC driver and a non-root user.
# One process serves the REST gateway, /olap and MCP on one port; the same
# `trio` command runs the catalog, converter and config tools.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first (cached unless the lock changes); README.md is read by the
# build backend, so it must be present for the project install below.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra odbc --extra llm --no-install-project

COPY trio/ ./trio/
RUN uv sync --frozen --no-dev --extra odbc --extra llm


FROM python:3.13-slim

WORKDIR /app

# Microsoft ODBC Driver 18 for SQL Server (the Python side, aioodbc/pyodbc,
# comes from the lock). Build with `--build-arg WITH_MSSQL_ODBC=0` to skip it.
ARG WITH_MSSQL_ODBC=1

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && if [ "$WITH_MSSQL_ODBC" = "1" ]; then \
         . /etc/os-release \
         && curl -fsSL -o /tmp/packages-microsoft-prod.deb \
              "https://packages.microsoft.com/config/debian/${VERSION_ID}/packages-microsoft-prod.deb" \
         && dpkg -i /tmp/packages-microsoft-prod.deb \
         && rm -f /tmp/packages-microsoft-prod.deb \
         && apt-get update \
         && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
              msodbcsql18 unixodbc libgssapi-krb5-2 ; \
       fi \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/catalogs /app/data /app/logs \
    && chown -R appuser:appuser /app

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/trio /app/trio
COPY --chown=appuser:appuser config/ /app/config/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production \
    LOG_LEVEL=INFO \
    LOG_FORMAT=json \
    CONFIG_PATH=/app/config/database.yaml

USER appuser

# Shell form on purpose: the port variable must expand here.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f "http://localhost:${API_PORT:-8000}/health" || exit 1

EXPOSE 8000

# Exec form runs no shell, so nothing here may rely on ${VAR} expansion; every
# setting comes from the environment and the YAML config.
CMD ["trio", "serve"]
