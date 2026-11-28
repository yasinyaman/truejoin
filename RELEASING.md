# Releasing 1.0.0

What is left before `v1.0.0` is tagged, as of 2026-11-27 (`main` at `394e732`).
The version is already `1.0.0` in `truejoin/__init__.py` and `helm/truejoin/Chart.yaml`;
the CHANGELOG heading stays `[1.0.0] - in progress` until the release is decided.

Each item says what it proves, where it stands, what it takes, and what is
needed from the maintainer. "Local" means it runs on this machine with nothing
sent anywhere.

## 1. A remote and a CI service

- **Proves:** that `ci.yml` passes on clean runners, every push, not only by
  hand; that `nightly.yml` (all four engines, the air-gapped image) and
  `docs.yml` (the site on `gh-pages`) work at all.
- **Stands:** the repository is pushed to `github.com/yasinyaman/truejoin`
  since 2026-11-15, public, with Actions on. The first `ci.yml` run, on the
  push of `2c15cc3`, passed every job (tests on 3.12 and 3.13, lint and
  types, the chart and docs, `pip-audit`, the release smoke against
  PostgreSQL, integration on PostgreSQL and MySQL). `docs.yml` publishes the
  site to `gh-pages` on every push to `main`, served by GitHub Pages at
  https://yaman.dev/truejoin/.
  `nightly.yml` has run every night since 2026-11-16, twelve runs, every one
  green, the last on `7e3f9f2` (2026-11-27): the integration suite on all
  four engines with SQL Server and Oracle required (a skip fails it), and
  the image built with `--network none` from the wheelhouse, then the
  release smoke against it. `ci.yml` runs on every pull request; it was green
  on every commit of yasinyaman/truejoin#4, merged as `394e732`.
- **Takes:** nothing in the workflows needs a secret.
- **From the maintainer:** nothing. The first nightly run on `394e732` is the
  first with the new SQL Server and Oracle identity code on `main`.

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
- **Stands:** done. `nightly.yml` runs the integration suite against SQL
  Server on x86-64 runners, green on every run since 2026-11-16. On
  2026-11-27 the suite also passed on an x86-64 machine against
  `docker/it`'s SQL Server 2022, with ODBC Driver 18 on the host and
  `WARP_REQUIRE_MSSQL=1` (73 passed), including the security policy on the
  caller's identity, which nightly had not yet run because it was not on
  `main`. On Apple Silicon it passed earlier under amd64 emulation
  (`make it-up`, `make it`).
- **From the maintainer:** nothing.

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

1. On the release commit, run every step of `ci.yml` again (local), unless
   `ci.yml` and the next `nightly.yml` run are green on that very commit:
   `uv run --frozen pytest` (with the coverage gate), `pytest bench/tests`,
   `uv run pytest tests -m integration`, ruff, format, mypy, `uv lock --check`,
   `make helm-check`, the strict docs build, `make smoke`, `pip-audit`.
2. Change the CHANGELOG heading to `[1.0.0] - <date>` (the maintainer's word).
3. Tag `v1.0.0` (the maintainer's word). The history already carries `v1.0.0` and
   `v1.1.0` from Fusion's own releases: rename or delete those local tags first, or
   choose another name, and push the release tag only after that.

Left out of 1.0.0 on purpose, and named in the CHANGELOG's *Not yet*: more
engines (Snowflake, Databricks, ClickHouse, Trino), and group administration (groups come from the directory or
the identity provider). Databases added at run time, local users made in the
console and the built-in assistant (off by default, `docs/docs/assistant.md`)
are in 1.0.0.
