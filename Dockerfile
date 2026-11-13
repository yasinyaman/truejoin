# syntax=docker/dockerfile:1
#
# One image for the whole product: one process serves the REST gateway, /olap
# and MCP on one port, and the same `truejoin` command runs the catalog, converter
# and config tools.
#
# Two ways to get the dependencies, chosen with --build-arg DEPS=...:
#
#   online      (default) uv resolves the locked set from the package index.
#   wheelhouse  nothing is fetched: wheels come from docker/wheelhouse/, which
#               `make wheelhouse` filled on a connected machine. With the ODBC
#               drivers vendored or left out, the whole build runs with
#               `docker build --network none`.
#
# Both install exactly what uv.lock names, with hashes checked.

# Pinned by digest: a tag can move, and an air-gapped site mirrors exactly this.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72
ARG DEPS=online

FROM ${UV_IMAGE} AS base
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./

# -- the wheels, for a later build with no network: `make wheelhouse` ----------
FROM base AS wheelhouse-build
RUN uv export --frozen --no-dev --extra odbc --extra llm --no-emit-project \
        --format requirements-txt -o /wheelhouse/requirements.txt \
    && python -m pip download --no-deps --require-hashes --only-binary=:all: \
        --disable-pip-version-check -r /wheelhouse/requirements.txt -d /wheelhouse

FROM scratch AS wheelhouse
COPY --from=wheelhouse-build /wheelhouse /

# -- dependencies ------------------------------------------------------------------
FROM base AS deps-online
RUN uv sync --frozen --no-dev --extra odbc --extra llm --no-install-project

FROM base AS deps-wheelhouse
RUN --mount=type=bind,source=docker/wheelhouse,target=/wheelhouse \
    uv venv \
    && uv pip install --offline --no-index --find-links /wheelhouse --require-hashes \
        -r /wheelhouse/requirements.txt

FROM deps-${DEPS} AS deps
# The application is not installed as a package: it is copied beside the
# environment and started through this launcher. Nothing has to be built, so
# the offline path needs no build backend. The guard matters: uvicorn starts
# its reload and worker processes by spawn, which imports this file again.
RUN printf '#!/app/.venv/bin/python\nfrom truejoin.adapters.inbound.cli.main import main\n\nif __name__ == "__main__":\n    main()\n' \
        > /app/.venv/bin/truejoin \
    && chmod 755 /app/.venv/bin/truejoin


FROM ${PYTHON_IMAGE} AS runtime

WORKDIR /app

# ODBC drivers (the Python side, aioodbc/pyodbc, comes from the lock).
#   WITH_MSSQL_ODBC   1       Microsoft ODBC Driver 18, from Microsoft's repository
#                     vendor  the .deb files in docker/vendor/mssql/ (no network)
#                     0       none
#   WITH_ORACLE_ODBC  1       Oracle Instant Client, from the .zip files (and the
#                             libaio .deb) in docker/vendor/oracle/. Never
#                             downloaded here: Oracle's licence is yours to accept.
#                     0       none
# See docker/vendor/README.md for what to put where.
ARG WITH_MSSQL_ODBC=1
ARG WITH_ORACLE_ODBC=0

RUN --mount=type=bind,source=docker/vendor,target=/vendor \
    set -eu; \
    if [ "$WITH_MSSQL_ODBC" = "1" ]; then \
        . /etc/os-release; \
        python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/ms.deb')" \
            "https://packages.microsoft.com/config/debian/${VERSION_ID}/packages-microsoft-prod.deb"; \
        dpkg -i /tmp/ms.deb; rm -f /tmp/ms.deb; \
        apt-get update; \
        ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
            msodbcsql18 unixodbc libgssapi-krb5-2; \
        rm -rf /var/lib/apt/lists/*; \
    elif [ "$WITH_MSSQL_ODBC" = "vendor" ]; then \
        ACCEPT_EULA=Y dpkg -i /vendor/mssql/*.deb; \
    fi; \
    if [ "$WITH_ORACLE_ODBC" = "1" ]; then \
        if ls /vendor/oracle/*.deb >/dev/null 2>&1; then dpkg -i /vendor/oracle/*.deb; fi; \
        ldconfig; ldconfig -p | grep -q 'libodbc.so.2' \
            || { echo 'unixODBC missing: add its .debs to docker/vendor/oracle (see its README)' >&2; exit 1; }; \
        mkdir -p /opt/oracle; \
        python -c "import glob, zipfile; [zipfile.ZipFile(z).extractall('/opt/oracle') for z in glob.glob('/vendor/oracle/*.zip')]"; \
        IC_DIR="$(find /opt/oracle -maxdepth 1 -name 'instantclient_*' | head -1)"; \
        DRIVER="$(find "$IC_DIR" -name 'libsqora.so*' | head -1)"; \
        test -n "$DRIVER"; \
        chmod -R a+rX /opt/oracle; \
        echo "$IC_DIR" > /etc/ld.so.conf.d/oracle-instantclient.conf; ldconfig; \
        printf '\n[Oracle ODBC driver]\nDescription=Oracle ODBC\nDriver=%s\nFileUsage=1\n' \
            "$DRIVER" >> /etc/odbcinst.ini; \
    fi; \
    useradd --create-home --uid 1000 appuser; \
    mkdir -p /app/catalogs /app/data /app/logs; \
    chown -R appuser:appuser /app

COPY --from=deps --chown=appuser:appuser /app/.venv /app/.venv
COPY --chown=appuser:appuser truejoin/ /app/truejoin/
COPY --chown=appuser:appuser config/ /app/config/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production \
    LOG_LEVEL=INFO \
    LOG_FORMAT=json \
    CONFIG_PATH=/app/config/database.yaml

USER appuser

# With the interpreter that is already here: the image carries no curl.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('API_PORT', '8000') + '/health', timeout=5)"]

EXPOSE 8000

# Exec form runs no shell, so nothing here may rely on ${VAR} expansion; every
# setting comes from the environment and the YAML config.
CMD ["truejoin", "serve"]
