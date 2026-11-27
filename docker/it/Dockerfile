# The integration tests' runner for SQL Server and Oracle, as a Linux image.
#
# Neither engine's ODBC driver is something a developer's laptop has, and
# Oracle publishes none for macOS on Apple Silicon. So the tests for those two
# run in here, against databases started beside it. PostgreSQL and MySQL need
# none of this: `uv run pytest tests -m integration` starts them itself.
#
# Built for whatever architecture the engine is: arm64 and x86-64 both have
# drivers. See docker/it/README.md.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim@sha256:531f855bda2c73cd6ef67d56b733b357cea384185b3022bd09f05e002cd144ca

ARG IC=https://download.oracle.com/otn_software/linux/instantclient/2360000
ARG IC_VERSION=23.6.0.24.10

RUN apt-get update && apt-get install -y --no-install-recommends \
        unixodbc odbcinst curl unzip ca-certificates gnupg libaio1 \
    && curl -fsSLo /tmp/ms.deb https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb \
    && dpkg -i /tmp/ms.deb && rm /tmp/ms.deb \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

# Oracle Instant Client: downloading it accepts Oracle's Free Use Terms. The
# driver's soname carries the client version, so it is discovered, not pinned.
RUN case "$(dpkg --print-architecture)" in arm64) arch=arm64 ;; *) arch=x64 ;; esac \
 && curl -fsSLo /tmp/basic.zip "$IC/instantclient-basiclite-linux.$arch-$IC_VERSION.zip" \
 && curl -fsSLo /tmp/odbc.zip  "$IC/instantclient-odbc-linux.$arch-$IC_VERSION.zip" \
 && mkdir -p /opt/oracle && unzip -q -o /tmp/basic.zip -d /opt/oracle \
 && unzip -q -o /tmp/odbc.zip -d /opt/oracle && rm /tmp/*.zip \
 && IC_DIR="$(find /opt/oracle -maxdepth 1 -name 'instantclient_*' | head -1)" \
 && DRIVER="$(find "$IC_DIR" -name 'libsqora.so*' | head -1)" \
 && test -n "$DRIVER" \
 && echo "$IC_DIR" > /etc/ld.so.conf.d/oracle-instantclient.conf && ldconfig \
 && printf '\n[Oracle 23 ODBC driver]\nDescription=Oracle ODBC\nDriver=%s\nFileUsage=1\n' \
      "$DRIVER" >> /etc/odbcinst.ini

# Exactly the locked dependency set, dev tools included, outside /app so the
# repository can be mounted there.
# No uv cache and no docs or pandas extras: the image is big enough as it is.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --extra dev --extra odbc --extra oracle --extra llm --no-install-project

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
