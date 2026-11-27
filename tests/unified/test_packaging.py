"""What ships around the code: the chart, the smoke stack, the image build.

Nothing here starts a container. The chart is rendered when `helm` is
installed (and skipped when it is not); the rest is read as text and YAML.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from truejoin import __version__
from truejoin.adapters.outbound.catalog_store.file_store import CatalogFileStore
from truejoin.adapters.outbound.yaml_config import load_config
from truejoin.application.settings import validate_production_config

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "helm" / "truejoin"
SNAPSHOTS = ROOT / "helm" / "snapshots"

helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def render(*args: str) -> str:
    done = subprocess.run(
        ["helm", "template", "truejoin", str(CHART), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def documents(rendered: str) -> dict[str, dict]:
    return {doc["kind"]: doc for doc in yaml.safe_load_all(rendered) if doc}


class TestChart:
    def test_it_carries_the_version_of_the_code(self):
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
        assert chart["version"] == __version__ and chart["appVersion"] == __version__

    @helm
    def test_it_lints(self):
        done = subprocess.run(
            ["helm", "lint", str(CHART)], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, done.stdout

    @helm
    @pytest.mark.parametrize(
        ("snapshot", "args"),
        [
            ("default.yaml", ()),
            ("hardened.yaml", ("-f", str(SNAPSHOTS / "hardened.values.yaml"))),
        ],
    )
    def test_the_rendering_is_the_one_that_was_reviewed(self, snapshot, args):
        """A changed template shows up as a diff to read. Regenerate with:

        helm template truejoin helm/truejoin [-f helm/snapshots/hardened.values.yaml] > helm/snapshots/<name>
        """
        assert render(*args) == (SNAPSHOTS / snapshot).read_text()

    @helm
    def test_the_pod_is_locked_down_by_default(self):
        deployment = documents(render())["Deployment"]["spec"]
        pod = deployment["template"]["spec"]
        (container,) = pod["containers"]
        assert deployment["replicas"] == 1 and deployment["strategy"] == {"type": "Recreate"}
        assert pod["securityContext"]["runAsNonRoot"] is True
        assert pod["automountServiceAccountToken"] is False
        assert container["securityContext"] == {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
        env = {e["name"]: e["value"] for e in container["env"]}
        assert env["APP_ENV"] == "production"
        # Read-only root: everything the process writes is a mounted volume.
        mounts = {m["mountPath"] for m in container["volumeMounts"]}
        assert {"/app/catalogs", "/app/data", "/app/logs", "/tmp"} <= mounts
        assert all(
            container[probe]["httpGet"]["path"] == "/health"
            for probe in ("startupProbe", "livenessProbe", "readinessProbe")
        )

    @helm
    def test_the_default_configuration_is_one_production_accepts(self):
        """The chart must not render a config the process then refuses to start with."""
        config = yaml.safe_load(documents(render())["ConfigMap"]["data"]["database.yaml"])
        from truejoin.application.settings import Settings

        settings = Settings.model_validate(config)
        assert validate_production_config(settings, "production", ["https://x.internal"]) == []
        assert settings.olap.temp_directory == "/app/data/tmp"

    @helm
    def test_secrets_are_files_and_never_in_the_configmap(self):
        rendered = render("-f", str(SNAPSHOTS / "hardened.values.yaml"))
        docs = documents(rendered)
        assert "Secret" not in docs  # an existing Secret is referenced, none is created
        config = docs["ConfigMap"]["data"]["database.yaml"]
        assert "${file:/run/secrets/truejoin/db-password}" in config
        pod = docs["Deployment"]["spec"]["template"]["spec"]
        secret = next(v for v in pod["volumes"] if v["name"] == "secrets")
        assert secret["secret"]["secretName"] == "truejoin-secrets"
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "secrets")
        assert mount == {"name": "secrets", "mountPath": "/run/secrets/truejoin", "readOnly": True}

    @helm
    def test_a_created_secret_is_mounted_too_and_the_notes_warn_about_it(self):
        docs = documents(render("--set", "secrets.create.db-password=x"))
        assert docs["Secret"]["stringData"] == {"db-password": "x"}
        volumes = docs["Deployment"]["spec"]["template"]["spec"]["volumes"]
        assert any(v["name"] == "secrets" for v in volumes)
        assert "secrets.existingSecret" in (CHART / "templates" / "NOTES.txt").read_text()

    @helm
    @pytest.mark.parametrize(
        "values", ["secrets:\n  create:\n    pin: 0755\n", "env:\n  BIG: 20000000\n"]
    )
    def test_a_number_that_would_be_written_changed_is_refused(self, values, tmp_path):
        file = tmp_path / "values.yaml"
        file.write_text(values)
        done = subprocess.run(
            ["helm", "template", "truejoin", str(CHART), "-f", str(file)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode != 0 and "must be a quoted string" in done.stderr

    @helm
    def test_the_network_policy_closes_everything_it_does_not_name(self):
        policy = documents(render("-f", str(SNAPSHOTS / "hardened.values.yaml")))["NetworkPolicy"]
        spec = policy["spec"]
        assert spec["policyTypes"] == ["Ingress", "Egress"]
        assert spec["ingress"][0]["from"] == [
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}}}
        ]
        dns, database = spec["egress"]
        assert {p["port"] for p in dns["ports"]} == {53}
        assert database == {
            "to": [{"ipBlock": {"cidr": "10.0.12.0/24"}}],
            "ports": [{"port": 1521, "protocol": "TCP"}],
        }
        assert "NetworkPolicy" not in documents(render())

    @helm
    def test_an_image_digest_wins_over_the_tag(self):
        def image(*args: str) -> str:
            pod = documents(render(*args))["Deployment"]["spec"]["template"]["spec"]
            return pod["containers"][0]["image"]

        assert image() == f"truejoin:{__version__}"
        assert image("--set", "image.tag=x", "--set", "image.digest=sha256:abc") == (
            "truejoin@sha256:abc"
        )


class TestSmokeStack:
    SMOKE = ROOT / "docker" / "smoke"

    def test_its_configuration_is_one_production_accepts(self, monkeypatch, tmp_path):
        for name in ("SMOKE_KEY_A", "SMOKE_KEY_B", "SMOKE_KEY_DPO"):
            monkeypatch.setenv(name, f"{name.lower()}-0123456789abcdef")
        text = (self.SMOKE / "config.yaml").read_text()
        secret = tmp_path / "db_password"
        secret.write_text("x\n")
        assert "${file:/run/secrets/db_password}" in text
        config = tmp_path / "config.yaml"
        config.write_text(text.replace("/run/secrets/db_password", str(secret)))
        settings = load_config(str(config))
        assert settings.databases[0].password == "x"
        assert validate_production_config(settings, "production", ["https://smoke.invalid"]) == []
        assert (settings.olap.grounding, settings.olap.terms) == ("repair", "on")

    def test_its_catalog_is_approved_labels_the_personal_column_and_has_certified_terms(self):
        catalog = CatalogFileStore(self.SMOKE / "catalogs").load_or_raise("smoke")
        assert catalog.status == "approved" and len(catalog.certified_terms) == 3
        assert catalog.tables["musteriler"].get_column("tc_kimlik").semantic_type == "tc_no"

    def test_the_stack_runs_the_image_read_only_without_privileges(self):
        compose = yaml.safe_load((self.SMOKE / "docker-compose.yml").read_text())
        api = compose["services"]["api"]
        assert api["read_only"] is True and api["cap_drop"] == ["ALL"]
        assert api["environment"]["APP_ENV"] == "production"


class TestImageBuild:
    DOCKERFILE = (ROOT / "Dockerfile").read_text()

    def test_base_images_are_pinned_by_digest(self):
        pinned = [
            line
            for line in self.DOCKERFILE.splitlines()
            if line.startswith(("ARG UV_IMAGE", "ARG PYTHON_IMAGE"))
        ]
        assert len(pinned) == 2 and all("@sha256:" in line for line in pinned)

    def test_the_offline_path_fetches_nothing(self):
        offline = self.DOCKERFILE.split("FROM base AS deps-wheelhouse")[1].split("FROM deps-")[0]
        assert "--offline" in offline and "--no-index" in offline and "--require-hashes" in offline

    def test_the_launcher_runs_main_only_as_a_script(self):
        # uvicorn's reload and workers spawn processes that import it again.
        launcher = next(
            line for line in self.DOCKERFILE.splitlines() if "/.venv/bin/python" in line
        )
        assert '\\nif __name__ == "__main__":\\n    main()' in launcher

    def test_an_oracle_image_without_unixodbc_does_not_build(self):
        oracle = self.DOCKERFILE.split('if [ "$WITH_ORACLE_ODBC" = "1" ]')[1].split(
            "fi; \\\n    useradd"
        )[0]
        assert "libodbc.so.2" in oracle and "exit 1" in oracle

    def test_it_runs_as_a_user_and_carries_no_curl(self):
        runtime = self.DOCKERFILE.split("AS runtime")[1]
        assert "USER appuser" in runtime
        assert "curl" not in runtime.replace("carries no curl", "")

    def test_what_an_offline_build_needs_is_not_committed(self):
        ignored = (ROOT / ".gitignore").read_text()
        assert "/docker/wheelhouse/" in ignored and "/docker/vendor/*" in ignored
        assert (ROOT / "docker" / "vendor" / "README.md").exists()


class TestSummariesForReaders:
    """README and llms.txt state facts search engines and models quote: keep them true."""

    SUMMARIES = (ROOT / "llms.txt", ROOT / "docs" / "docs" / "llms.txt")

    def test_llms_txt_lists_every_tool_and_no_other(self):
        import re

        from truejoin.application.olap.tool_schemas import TOOL_NAMES

        for path in self.SUMMARIES:
            text = path.read_text()
            listed = re.search(r"(\d+) MCP tools: ([^.]+)\.", text)
            assert listed, path
            names = [n.strip() for n in listed.group(2).split(",")]
            assert int(listed.group(1)) == len(TOOL_NAMES) and set(names) == set(TOOL_NAMES), path

    def test_the_two_summaries_say_the_same(self):
        site, repo = (path.read_text() for path in reversed(self.SUMMARIES))
        heading = "## Docs"
        assert site.split(heading)[0] == repo.split(heading)[0]

    def test_the_readme_points_at_files_that_exist_and_names_the_console_screens(self):
        import re

        readme = (ROOT / "README.md").read_text()
        targets = re.findall(r"\]\(([^)\s#]+)(?:#[^)]*)?\)", readme)
        local = [t for t in targets if not t.startswith(("http://", "https://", "mailto:"))]
        assert len(local) > 15
        assert [t for t in local if not (ROOT / t).exists()] == []
        for screen in ("Query", "Catalog", "Administration", "Docs"):
            assert f"| **{screen}** |" in readme, screen
        # The docs site's home page plays the same films from its own assets.
        home = (ROOT / "docs" / "docs" / "index.md").read_text()
        for film in re.findall(r'src="(assets/[^"]+)"', home):
            assert (ROOT / "docs" / "docs" / film).exists(), film

    def test_the_demo_films_stay_small(self):
        """They live in the repository's history for good: a film that grows is cut, not kept."""
        films = sorted((ROOT / "docs" / "docs" / "assets").glob("*-demo.*"))
        # One film: the console's recording, then Claude's (scripts/demo/join.sh).
        assert {f.name for f in films} == {"truejoin-demo.gif", "truejoin-demo.mp4"}
        megabyte = 1024 * 1024
        for film in films:
            most = 4 if film.suffix == ".gif" else 6
            assert film.stat().st_size <= most * megabyte, film.name

    def test_the_recorded_session_is_a_session_and_carries_no_key(self):
        """The agent's session in the README is one that ran (scripts/demo/README.md),
        kept without what is about the machine it ran on, and without a key's value."""
        import json

        demo = ROOT / "scripts" / "demo"
        text = (demo / "claude-session.json").read_text()
        session = json.loads(text)
        kinds = [event["type"] for event in session["events"]]
        assert kinds.count("tool_use") == kinds.count("tool_result") > 0 and kinds[-1] == "text"
        assert session["ended"] == "success" and session["prompt"]
        keys = [
            k["key"]
            for k in yaml.safe_load((demo / "config.yaml").read_text())["settings"]["auth"][
                "api_keys"
            ]
        ]
        assert len(keys) == 3
        for secret in (*keys, "X-API-Key", "session_id", "/home/", "/tmp/"):
            assert secret not in text, secret
        # The client reaches the sample setup as the key the film says it is.
        client = json.loads((demo / "mcp.json").read_text())["mcpServers"]["truejoin"]
        assert client["headers"]["X-API-Key"] in keys

    def test_the_sample_setup_starts_from_one_file_and_stays_on_this_machine(self):
        """scripts/demo/docker-compose.yml is what the README tells a newcomer to run."""
        import re

        demo = ROOT / "scripts" / "demo"
        compose = yaml.safe_load((demo / "docker-compose.yml").read_text())
        api = compose["services"]["api"]
        # Two databases on two engines, and the server waits for both.
        assert compose["services"]["db"]["image"].startswith("postgres:")
        assert compose["services"]["crm"]["image"].startswith("mysql:")
        assert set(api["depends_on"]) == {"db", "crm"}
        # The sample keys are public: the port must not be reachable from the network.
        assert all(str(port).startswith("127.0.0.1:") for port in api["ports"])
        # Not production (it refuses sample keys), not development (it reloads).
        assert api["environment"]["APP_ENV"] not in ("production", "development")
        # Every file it mounts is in the repository, where the compose file says it is.
        mounted = [
            v.split(":")[0]
            for s in compose["services"].values()
            for v in s.get("volumes", [])
            if v.startswith(".")
        ]
        assert len(mounted) >= 3
        assert [m for m in mounted if not (demo / m).resolve().exists()] == []
        # What it sets in the environment is what config.yaml reads, with a default
        # for running from source.
        config = (demo / "config.yaml").read_text()
        for name in ("DEMO_DB_HOST", "DEMO_MYSQL_HOST", "DEMO_DIR"):
            assert name in api["environment"] and re.search(rf"\$\{{{name}:[^}}]+\}}", config), name
        # No key is written in it: the one the console's stores need is made on first start.
        text = (demo / "docker-compose.yml").read_text()
        assert "TRUEJOIN_SECRET_KEY=" in text and "secret.key" in text
        assert not re.search(r"[A-Za-z0-9_-]{43}=", text)
        # The command the README gives is the one the Makefile and CI run.
        command = "docker compose -f scripts/demo/docker-compose.yml"
        assert command in (ROOT / "README.md").read_text()
        assert command in (ROOT / "Makefile").read_text()
        assert "make demo-check" in (ROOT / ".github" / "workflows" / "ci.yml").read_text()

    def test_the_sample_catalogs_are_what_their_script_writes(self):
        """scripts/demo/catalogs is checked in: it must be what make_catalog.py builds,
        the smoke check's own catalog among them, or the two setups drift apart."""
        import json
        import runpy

        demo = ROOT / "scripts" / "demo"

        def told(catalog) -> dict:
            said = json.loads(catalog.model_dump_json())
            return {k: v for k, v in said.items() if k not in ("generated_at", "updated_at")}

        def kept(path: Path) -> dict:
            said = json.loads(path.read_text())
            return {k: v for k, v in said.items() if k not in ("generated_at", "updated_at")}

        script = runpy.run_path(str(demo / "make_catalog.py"))
        assert kept(demo / "catalogs" / "crm" / "catalog.json") == told(script["build_crm"]())
        smoke = ROOT / "docker" / "smoke"
        built = runpy.run_path(str(smoke / "make_catalog.py"))["build"]()
        assert kept(demo / "catalogs" / "smoke" / "catalog.json") == told(built)
        assert kept(smoke / "catalogs" / "smoke" / "catalog.json") == told(built)

    def test_the_sample_report_joins_both_databases_and_any_caller_may_be_offered_it(self):
        import re
        import runpy

        from truejoin.domain.catalog.examples import has_literals

        demo = ROOT / "scripts" / "demo"
        crm = runpy.run_path(str(demo / "make_catalog.py"))["build_crm"]()
        assert crm.status.value == "approved" and all(t.certified for t in crm.terms.values())
        for name in runpy.run_path(str(demo / "report.py"))["ANALYSES"]:
            analysis = crm.examples[name]
            assert analysis.certified, name
            # One table of each database at least: the join is across engines.
            assert {t.split(".")[0] for t in analysis.tables} == {"smoke", "crm"}, name
            # A constant would hide it from a caller held to row rules or masks.
            assert not has_literals(analysis.sql), name
        # The second database has a labelled personal column, and a rule that masks it.
        config = yaml.safe_load((demo / "config.yaml").read_text())
        assert {d["name"]: d["type"] for d in config["databases"]} == {
            "smoke": "postgresql",
            "crm": "mysql",
        }
        labelled = {
            c.semantic_type for t in crm.tables.values() for c in t.columns if c.semantic_type
        }
        assert labelled and labelled <= set(config["settings"]["masking"]["rules"])
        # Every table the MySQL's file makes is in its catalog.
        made = re.findall(r"CREATE TABLE (\w+)", (demo / "crm.sql").read_text())
        assert sorted(made) == sorted(crm.tables)

    def test_the_readme_and_citation_carry_the_version_and_tool_count(self):
        from truejoin.application.olap.tool_schemas import TOOL_NAMES

        assert f"{len(TOOL_NAMES)} tools" in (ROOT / "README.md").read_text()
        citation = yaml.safe_load((ROOT / "CITATION.cff").read_text())
        assert citation["version"] == __version__
