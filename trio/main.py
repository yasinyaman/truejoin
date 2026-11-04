"""Entrypoint: the `warp` console script and the `trio.main:app` ASGI target.

This is the only HTTP-facing module allowed to touch the composition root.
"""

from trio.adapters.inbound.http.app import create_app
from trio.adapters.outbound.yaml_config import load_config
from trio.application.container import Container
from trio.application.settings import RuntimeEnv
from trio.bootstrap_warp import build_container
from trio.observability.warp_logging import setup_logging

ENV = RuntimeEnv.from_environ()


def container_factory() -> Container:
    """Configure logging, load the YAML config and wire the default adapters."""
    setup_logging(level=ENV.log_level, json_format=ENV.log_json)
    return build_container(load_config(ENV.config_path))


app = create_app(container_factory=container_factory, env=ENV)


def main() -> None:
    """Run the API with uvicorn (the `warp` console script)."""
    import uvicorn

    reload = ENV.app_env == "development"
    uvicorn.run(
        "trio.main:app",
        host=ENV.api_host,
        port=ENV.api_port,
        workers=ENV.api_workers if not reload else 1,
        reload=reload,
        access_log=not ENV.is_production,
    )


if __name__ == "__main__":
    main()
