"""The ASGI target (`trio.main:app`), which `trio serve` hands to uvicorn.

An import string is what lets uvicorn reload and fork workers; everything the
app needs is read from the environment here and built when the app starts.
"""

from trio.adapters.inbound.http.app import create_app
from trio.application.container import Container
from trio.application.settings import RuntimeEnv
from trio.bootstrap import build_container, load_settings
from trio.observability.warp_logging import setup_logging

ENV = RuntimeEnv.from_environ()


def container_factory() -> Container:
    """Configure logging, load the config and wire the default adapters."""
    setup_logging(level=ENV.log_level, json_format=ENV.log_json)
    return build_container(load_settings(ENV.config_path))


app = create_app(container_factory=container_factory, env=ENV)
