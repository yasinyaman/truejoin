# Vendored ODBC drivers

The image build never downloads Oracle software, and a build with no network
cannot download Microsoft's either. What it needs is put here by hand, on a
connected machine, and travels with the source. Everything in this directory
except this file is ignored by git.

## SQL Server: `docker/vendor/mssql/` (`--build-arg WITH_MSSQL_ODBC=vendor`)

The driver's `.deb` and the packages it depends on, for Debian 12 (bookworm)
and the image's architecture:

```bash
docker run --rm -v "$PWD/docker/vendor/mssql:/out" -w /out python:3.13-slim-bookworm sh -c '
  apt-get update && apt-get install -y curl gnupg &&
  curl -fsSLo /tmp/ms.deb https://packages.microsoft.com/config/debian/12/packages-microsoft-prod.deb &&
  dpkg -i /tmp/ms.deb && apt-get update &&
  apt-get download msodbcsql18 unixodbc odbcinst libodbc2 libodbcinst2 unixodbc-common libgssapi-krb5-2 libkrb5-3 libk5crypto3 libkrb5support0 libcom-err2 libkeyutils1'
```

Installing them accepts Microsoft's EULA for the ODBC driver on your behalf
(`ACCEPT_EULA=Y`), as the online build does.

## Oracle: `docker/vendor/oracle/` (`--build-arg WITH_ORACLE_ODBC=1`)

From Oracle's Instant Client downloads, for Linux and the image's
architecture, after accepting Oracle's licence there:

- `instantclient-basiclite-linux.<arch>-<version>.zip`
- `instantclient-odbc-linux.<arch>-<version>.zip`
- the `.deb`s of `libaio1` and of unixODBC, which pyodbc and the driver load,
  for Debian 12 (`apt-get download libaio1 unixodbc odbcinst libodbc2
  libodbcinst2 unixodbc-common`); a build with `WITH_MSSQL_ODBC` too already
  has unixODBC, and one without it stops when they are missing

The build unpacks them under `/opt/oracle` and registers the driver as
`Oracle ODBC driver`.
