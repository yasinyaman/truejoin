# Releasing 1.0.0

What is left before `v1.0.0` is tagged, as of 2026-11-09 (main at `7a8d4ff`).
The version is already `1.0.0` in `trio/__init__.py` and `helm/trio/Chart.yaml`;
the CHANGELOG heading stays `[1.0.0] - in progress` until the release is decided.

Each item says what it proves, where it stands, what it takes, and what is
needed from the maintainer. "Local" means it runs on this machine with nothing
sent anywhere.

## 1. A remote and a CI service

- **Proves:** that `ci.yml` passes on clean runners, every push, not only by
  hand; that `nightly.yml` (all four engines, the air-gapped image) and
  `docs.yml` (the site on `gh-pages`) work at all.
- **Stands:** there is no remote. Every step of `ci.yml` was run by hand on
  2026-11-08 and passed (tests on 3.12 and 3.13 with coverage 84.5%, the
  bench harness, integration on PostgreSQL, MySQL and MariaDB, lint, format,
  mypy, the lock, the chart snapshots, the strict docs build, `make smoke`,
  `pip-audit`). Since then `make smoke` passed again (24 of 24, 2026-11-09)
  and the unit, bench and docs checks pass on every commit; the integration
  suite, the chart snapshots and `pip-audit` were not run again.
  `nightly.yml` and `docs.yml` have never run.
- **Takes:** a repository to push to, Actions enabled, and for `docs.yml`
  GitHub Pages from the `gh-pages` branch (the workflow needs
  `contents: write`). Nothing in the workflows needs a secret.
- **From the maintainer:** where it lives (account or organisation, name,
  public or private) and the go-ahead to push. Until then, the steps of
  `ci.yml` can be run again by hand on the release commit.

## 2. The Helm chart, installed on a cluster

- **Proves:** that the rendered chart (`helm/snapshots/`, reviewed and checked
  in CI) actually starts: the pod becomes ready under the hardened settings
  (non-root, read-only root filesystem, no capabilities, no service-account
  token) and the network policy lets through what it names.
- **Stands:** `helm lint` and the snapshot diff pass; it has never been
  installed.
- **Takes:** a cluster, the image loaded into it, a PostgreSQL it can reach.
  `kind`, `helm` and `kubectl` are installed on this machine, so a local
  kind cluster (v0.33 here) with the smoke's PostgreSQL is enough for a
  first install; whether the network policy is enforced depends on the
  cluster's CNI, and has to be checked rather than assumed.
- **From the maintainer:** whether a local kind install is enough for 1.0.0,
  or which real cluster to use.

## 3. SQL Server on x86-64 hardware

- **Proves:** the SQL Server gateway against the engine on the hardware it is
  built for, not under emulation.
- **Stands:** the integration suite has passed against SQL Server through
  `make it-up` and `make it`, under amd64 emulation on this machine (Apple
  Silicon); `uv run pytest tests -m integration` alone skips it here.
  `nightly.yml` runs it on x86-64 and has never run.
- **Takes:** CI (item 1), or any x86-64 machine with Docker and the Microsoft
  ODBC driver.
- **From the maintainer:** whether emulation is enough for 1.0.0, or to wait
  for the nightly run once CI exists.

## 4. The vendored ODBC builds

- **Proves:** that the image builds with the drivers coming from
  `docker/vendor/` instead of the network: `WITH_MSSQL_ODBC=vendor` and
  `WITH_ORACLE_ODBC=1`, for an air-gapped site that needs SQL Server or Oracle.
- **Stands:** the Dockerfile paths are written and have never been built. The
  online driver install is what the test runner and the default image use.
- **Takes:** the Microsoft ODBC Driver 18 `.deb` files and the Oracle Instant
  Client `.zip` files (plus `libaio` and unixODBC `.deb`s) in
  `docker/vendor/mssql/` and `docker/vendor/oracle/`, as
  `docker/vendor/README.md` lists; then the two builds and a smoke against
  each.
- **From the maintainer:** agreement to download them, which accepts
  Microsoft's EULA and Oracle's Free Use Terms, or the files themselves. They
  are not committed either way.

## 5. The release itself

Once the items above are done, or explicitly left out of 1.0.0:

1. On the release commit, run every step of `ci.yml` again (local):
   `uv run --frozen pytest` (with the coverage gate), `pytest bench/tests`,
   `uv run pytest tests -m integration`, ruff, format, mypy, `uv lock --check`,
   `make helm-check`, the strict docs build, `make smoke`, `pip-audit`.
2. Change the CHANGELOG heading to `[1.0.0] - <date>` (the maintainer's word).
3. Tag `v1.0.0` (the maintainer's word), and push the tag only if item 1 exists.

Left out of 1.0.0 on purpose, and named in the CHANGELOG's *Not yet*: an
in-product text-to-SQL agent, more engines (Snowflake, Databricks,
ClickHouse, Trino), runtime connection management, user and group admin.
