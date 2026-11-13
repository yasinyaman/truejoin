# Releasing 1.0.0

What is left before `v1.0.0` is tagged, as of 2026-11-09 (main at `7a8d4ff`).
The version is already `1.0.0` in `truejoin/__init__.py` and `helm/truejoin/Chart.yaml`;
the CHANGELOG heading stays `[1.0.0] - in progress` until the release is decided.

Each item says what it proves, where it stands, what it takes, and what is
needed from the maintainer. "Local" means it runs on this machine with nothing
sent anywhere.

## 1. A remote and a CI service

- **Proves:** that `ci.yml` passes on clean runners, every push, not only by
  hand; that `nightly.yml` (all four engines, the air-gapped image) and
  `docs.yml` (the site on `gh-pages`) work at all.
- **Stands:** there is no remote. Every step of `ci.yml` was run again by
  hand on 2026-11-09 on main at `50b0d18` (macOS, Apple Silicon), and each
  passed: the tests on Python 3.12.11 and 3.13.9 (3569 each, coverage 84.76%
  against the 80% gate), the bench harness (183), integration on PostgreSQL,
  MySQL and MariaDB (35 passed; the 36 skipped are SQL Server and Oracle, as
  `ci.yml` expects), ruff, format, mypy, `uv lock --check`, `make helm-check`
  (lint and both snapshots), the strict docs build, `pip-audit` on the locked
  runtime set (no known vulnerabilities) and `make smoke` (24 of 24).
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
- **Stands:** done on a local kind cluster on 2026-11-09 (kind v0.33,
  Kubernetes v1.37, arm64), with the image built from `ed8bfa8`
  (`WITH_MSSQL_ODBC=0`) and PostgreSQL holding `docker/smoke/init.sql` in a
  namespace of its own. `helm install` with the network policy on came up
  ready (Recreate, both PVCs bound on `local-path`). In the pod: uid 1000,
  `/app` read-only, no service-account token mounted. A client pod with the
  allowed label got `/health` (database connected) and a query with the
  analyst key; one without the label timed out. From the pod, PostgreSQL
  was reachable and the Kubernetes API timed out: kind's CNI enforces the
  policy. A `rollout restart` came back ready. The audit trail (one event, no
  SQL) was on file and on stdout; with `persistence.logs` off, as by default,
  the file goes with the pod, as `values.yaml` says. The cluster was deleted.
- **Not covered:** an ingress controller and TLS, Vault injection, image by
  digest from a registry, a real storage class, x86-64 nodes.
- **From the maintainer:** whether this local install is enough for 1.0.0.

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

1. On the release commit, if it is not `50b0d18`, run every step of `ci.yml`
   again (local):
   `uv run --frozen pytest` (with the coverage gate), `pytest bench/tests`,
   `uv run pytest tests -m integration`, ruff, format, mypy, `uv lock --check`,
   `make helm-check`, the strict docs build, `make smoke`, `pip-audit`.
2. Change the CHANGELOG heading to `[1.0.0] - <date>` (the maintainer's word).
3. Tag `v1.0.0` (the maintainer's word), and push the tag only if item 1 exists.

Left out of 1.0.0 on purpose, and named in the CHANGELOG's *Not yet*: an
in-product text-to-SQL agent, more engines (Snowflake, Databricks,
ClickHouse, Trino), runtime connection management, user and group admin.
