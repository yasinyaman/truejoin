"""The OLAP router on a bare app, for route tests that need no gateway or auth."""

from dataclasses import replace

from fastapi import FastAPI

from truejoin.adapters.inbound.rest import install_error_handlers, olap_router


def create_app(fusion, settings=None) -> FastAPI:
    if settings is not None:
        fusion = replace(fusion, settings=settings)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(olap_router(fusion, rate_limit=fusion.settings.rate_limit))
    return app
