"""Starlette middleware for the REST adapter."""

from trio.adapters.inbound.rest.middleware.auth import AuthMiddleware
from trio.adapters.inbound.rest.middleware.logging import StructuredLoggingMiddleware

__all__ = ["AuthMiddleware", "StructuredLoggingMiddleware"]
