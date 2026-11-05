# SQL Server and Oracle integration tests, on any machine

`uv run pytest tests -m integration` starts PostgreSQL and MySQL by itself.
SQL Server and Oracle also need an ODBC driver, which a laptop rarely has and
which Oracle does not publish for macOS on Apple Silicon. `make it-up` and
`make it` run those two in containers instead:

```bash
make it-up        # start Oracle and SQL Server, build the runner (first time: several GB)
make it           # the integration suite against both
make it-down      # stop and remove them
```

- **Oracle** is `gvenzl/oracle-free`, which has arm64 and x86-64 images.
- **SQL Server** is x86-64 only. On Apple Silicon it runs under emulation,
  which needs Rosetta in Docker Desktop (Settings > General > "Use Rosetta
  for x86_64/amd64 emulation"); under plain QEMU the engine does not start.
- The runner image installs Microsoft's ODBC driver and Oracle Instant
  Client. Building it downloads both and accepts their licence terms
  (`ACCEPT_EULA=Y`, Oracle Free Use Terms).

Both databases up at once want about 10 GB of Docker disk and 6 GB of
memory. With less, start one at a time
(`docker compose -f docker/it/docker-compose.yml up -d --wait oracle`) and
run its tests with `--no-deps` and `-k oracle`.

The tests reach the databases through `WARP_ORACLE_HOST` and
`WARP_MSSQL_HOST`, which is also how to point them at a server you already
run. With `WARP_REQUIRE_ORACLE=1` and `WARP_REQUIRE_MSSQL=1` an engine that
cannot be reached is a failure, not a skip. On x86-64 CI none of this is
needed: the nightly workflow installs the drivers on the runner and
testcontainers starts the databases.
