"""The OLAP REST routes, mounted by the one ASGI app (``adapters.inbound.http``)."""

from truejoin.adapters.inbound.rest.routes import install_error_handlers, olap_router

__all__ = ["install_error_handlers", "olap_router"]
