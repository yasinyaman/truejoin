# Contributing to Truejoin

Thanks for your interest in contributing! This guide covers local setup and the
checks your change must pass.

## Development setup

```bash
git clone https://github.com/yasinyaman/truejoin.git
cd truejoin
uv sync --all-extras --frozen      # the hash-checked uv.lock, as CI installs it
```

Copy `.env.example` to `.env` and fill in values for local Docker work:

```bash
cp .env.example .env
make docker-up                     # API + PostgreSQL + MySQL + Adminer
```

## Quality gates (all enforced in CI)

Run them locally before opening a PR (`make lint` runs the three lint steps):

```bash
uv run --frozen pytest --cov=truejoin         # tests must pass; coverage must stay >= 80%
uv run --frozen pytest bench/tests            # the benchmark harness
uv run --frozen ruff check truejoin tests bench demo
uv run --frozen ruff format --check truejoin tests bench demo
uv run --frozen mypy truejoin                 # strict on domain, ports and application
uv lock --check                               # uv.lock matches pyproject.toml
uv run --frozen mkdocs build --strict         # the docs build without warnings
```

The access rewrite is fuzzed by three tests named `*fuzz*`. A plain run takes a
fixed set of examples; after a change to the rewriter, the planner or slicing,
run many random ones:

```bash
TRUEJOIN_FUZZ_EXAMPLES=2000 uv run --frozen pytest -k fuzz
```

The layering is checked by a test, `tests/fusion/architecture/test_dependency_rules.py`,
which walks every import (lazy ones too), so it runs with `pytest`.

CI runs the tests on Python 3.12 and 3.13 (`.github/workflows/ci.yml`), and also
`make helm-check`, `make smoke` (the built image against a real PostgreSQL) and
`pip-audit` against the locked runtime set, which fails the build on a known
vulnerability.

`make test-integration` (`uv run pytest tests -m integration`) runs the real-database
suite through testcontainers (Docker required). PostgreSQL and MySQL always run; SQL
Server and Oracle run in containers with their ODBC drivers (`make it-up`, `make it`,
`make it-down`; see `docker/it/README.md`), or against a server you run yourself:
`WARP_MSSQL_HOST=... WARP_MSSQL_PASSWORD=... uv run pytest tests -m integration -k mssql`.

### Dependencies

Version ranges live in `pyproject.toml`; exact, hash-pinned versions live in
`uv.lock`, used by CI and the Docker image. After changing a dependency in
`pyproject.toml`, run `uv lock` (or `make lock`) and commit both files together.

### Conventions

- **Typing:** modern syntax only (`dict[str, Any]`, `list[...]`, `X | None`).
  `domain`, `ports` and `application` are checked strictly: annotate new code fully.
- **Architecture:** hexagonal. `truejoin.domain` is pure Python; `truejoin.ports`
  holds the Protocols; `truejoin.application` uses only those two; adapters implement
  the ports (`truejoin.adapters.outbound`) or drive the application
  (`truejoin.adapters.inbound`: HTTP, MCP, CLI, SDK); `truejoin.bootstrap` is the one
  place that wires them together. New I/O goes behind a port and is wired in
  `truejoin/bootstrap.py`. CLAUDE.md has the full dependency-rule table; see also
  [ADR-0007](docs/docs/adr/0007-hexagonal-architecture.md) and
  [ADR-0010](docs/docs/adr/0010-one-package-one-process.md).
- **Tests:** keep them hermetic: no real database, network or LLM calls. Use the
  fakes in `tests/fusion/fakes` (sources, gateways, schedulers, a fake Warp transport)
  rather than mocks. New behavior needs tests; coverage must not drop below 80%.
- **SQL:** never interpolate values into SQL; use `:name` placeholders bound by
  `truejoin.adapters.outbound.db.params`. Dynamic identifiers go through
  `truejoin.adapters.outbound.db.identifiers`.
- **Console text:** every string the console shows goes through `tr(...)` and needs a
  Turkish translation (`scripts/console_tr/`, then `python scripts/build_console_tr.py
  scripts/console_tr` and `python scripts/console_i18n.py check`).
- **Formatting:** `ruff format` (run `make format`); no other formatter.
- **Commits:** small, focused commits with clear messages.

## Pull requests

1. Branch from `main`.
2. Make your change with tests and docs.
3. Ensure every gate above is green.
4. Open a PR describing the change and rationale.

## Reporting bugs / security issues

For functional bugs, open an issue. For security vulnerabilities, **do not** open
a public issue: follow [SECURITY.md](SECURITY.md).
