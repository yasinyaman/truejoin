"""FastAPI REST adapter."""

from trio.adapters.inbound.rest.app import create_app

__all__ = ["create_app"]
