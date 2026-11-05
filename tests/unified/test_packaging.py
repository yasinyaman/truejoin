"""What ships around the code: the chart, the smoke stack, the image build.

Nothing here starts a container. The chart is rendered when `helm` is
installed (and skipped when it is not); the rest is read as text and YAML.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from trio import __version__
from trio.adapters.outbound.catalog_store.file_store import CatalogFileStore
from trio.adapters.outbound.yaml_config import load_config
from trio.application.settings import validate_production_config

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "helm" / "trio"
SNAPSHOTS = ROOT / "helm" / "snapshots"

helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm is not installed")


def render(*args: str) -> str:
    done = subprocess.run(
        ["helm", "template", "trio", str(CHART), *args], capture_output=True, text=True, check=False
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

        helm template trio helm/trio [-f helm/snapshots/hardened.values.yaml] > helm/snapshots/<name>
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
        from trio.application.settings import Settings

        settings = Settings.model_validate(config)
        assert validate_production_config(settings, "production", ["https://x.internal"]) == []
        assert settings.olap.temp_directory == "/app/data/tmp"

    @helm
    def test_secrets_are_files_and_never_in_the_configmap(self):
        rendered = render("-f", str(SNAPSHOTS / "hardened.values.yaml"))
        docs = documents(rendered)
        assert "Secret" not in docs  # an existing Secret is referenced, none is created
        config = docs["ConfigMap"]["data"]["database.yaml"]
        assert "${file:/run/secrets/trio/db-password}" in config
        pod = docs["Deployment"]["spec"]["template"]["spec"]
        secret = next(v for v in pod["volumes"] if v["name"] == "secrets")
        assert secret["secret"]["secretName"] == "trio-secrets"
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "secrets")
        assert mount == {"name": "secrets", "mountPath": "/run/secrets/trio", "readOnly": True}

    @helm
    def test_a_created_secret_is_mounted_too_and_the_notes_warn_about_it(self):
        docs = documents(render("--set", "secrets.create.db-password=x"))
        assert docs["Secret"]["stringData"] == {"db-password": "x"}
        volumes = docs["Deployment"]["spec"]["template"]["spec"]["volumes"]
        assert any(v["name"] == "secrets" for v in volumes)
        assert "secrets.existingSecret" in (CHART / "templates" / "NOTES.txt").read_text()

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

        assert image() == f"trio-engine:{__version__}"
        assert image("--set", "image.tag=x", "--set", "image.digest=sha256:abc") == (
            "trio-engine@sha256:abc"
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

    def test_it_runs_as_a_user_and_carries_no_curl(self):
        runtime = self.DOCKERFILE.split("AS runtime")[1]
        assert "USER appuser" in runtime
        assert "curl" not in runtime.replace("carries no curl", "")

    def test_what_an_offline_build_needs_is_not_committed(self):
        ignored = (ROOT / ".gitignore").read_text()
        assert "/docker/wheelhouse/" in ignored and "/docker/vendor/*" in ignored
        assert (ROOT / "docker" / "vendor" / "README.md").exists()
