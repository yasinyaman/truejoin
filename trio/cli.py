"""Entrypoint: the `warp-catalog` console script.

Injects the composition root into the Click group so the CLI adapter itself
never imports infrastructure.
"""

from trio.adapters.inbound.cli.catalog import main
from trio.adapters.outbound.yaml_config import load_config
from trio.application.container import Container
from trio.bootstrap import build_container


def container_factory(config_path: str) -> Container:
    """Load the YAML config at `config_path` and wire the default adapters."""
    return build_container(load_config(config_path))


main.context_settings = {**main.context_settings, "obj": {"container_factory": container_factory}}

__all__ = ["container_factory", "main"]

if __name__ == "__main__":
    main()
