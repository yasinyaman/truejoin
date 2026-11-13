"""The ASGI target (`truejoin.main:app`), which `truejoin serve` hands to uvicorn.

An import string is what lets uvicorn reload and fork workers; everything the
app needs is read from the environment here and built when the app starts.
"""

from truejoin.adapters.inbound.http.app import create_app
from truejoin.application.container import Container
from truejoin.application.settings import RuntimeEnv
from truejoin.bootstrap import build_container, load_settings
from truejoin.observability.warp_logging import setup_logging

ENV = RuntimeEnv.from_environ()


def container_factory() -> Container:
    """Configure logging, load the config and wire the default adapters."""
    setup_logging(level=ENV.log_level, json_format=ENV.log_json)
    return build_container(load_settings(ENV.config_path))


app = create_app(container_factory=container_factory, env=ENV)
