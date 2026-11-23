"""ETL-Agent FastAPI application entry point."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from redis import Redis
from sqlalchemy import text

from etl_agent.api.v1.approval import router as approval_router
from etl_agent.api.v1.auth import router as auth_router
from etl_agent.api.v1.benchmark import router as benchmark_router
from etl_agent.api.v1.connections import router as connections_router
from etl_agent.api.v1.departments import router as departments_router
from etl_agent.api.v1.execution_runs import router as execution_runs_router
from etl_agent.api.v1.knowledge import router as knowledge_router
from etl_agent.api.v1.permissions import router as permissions_router
from etl_agent.api.v1.pipelines import router as pipelines_router
from etl_agent.api.v1.preparations import router as preparations_router
from etl_agent.api.v1.project import router as project_router
from etl_agent.api.v1.tenant import router as tenant_router
from etl_agent.core.error_handlers import register_exception_handlers
from etl_agent.core.openapi_zh import INFO_DESCRIPTION, INFO_TITLE, localize_openapi
from etl_agent.db.session import engine
from etl_agent.settings import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title=INFO_TITLE,
    description=INFO_DESCRIPTION,
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)

BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "static" / "ui"

# Register v1 API routers
app.include_router(auth_router, prefix=settings.api_v1_prefix)
app.include_router(tenant_router, prefix=settings.api_v1_prefix)
app.include_router(project_router, prefix=settings.api_v1_prefix)
app.include_router(connections_router, prefix=settings.api_v1_prefix)
app.include_router(pipelines_router, prefix=settings.api_v1_prefix)
app.include_router(approval_router, prefix=settings.api_v1_prefix)
app.include_router(preparations_router, prefix=settings.api_v1_prefix)
app.include_router(execution_runs_router, prefix=settings.api_v1_prefix)
app.include_router(knowledge_router, prefix=settings.api_v1_prefix)
app.include_router(benchmark_router, prefix=settings.api_v1_prefix)
app.include_router(departments_router, prefix=settings.api_v1_prefix)
app.include_router(permissions_router, prefix=settings.api_v1_prefix)

# Register global exception handlers for uniform error responses
register_exception_handlers(app)

_original_openapi = app.openapi


def _localized_openapi() -> dict[str, Any]:
    """Return the OpenAPI schema with every human-readable text in Chinese."""
    if app.openapi_schema:
        return app.openapi_schema
    app.openapi_schema = localize_openapi(_original_openapi())
    return app.openapi_schema


app.openapi = _localized_openapi  # type: ignore[method-assign]


def _check_database() -> bool:
    """Return whether PostgreSQL accepts a simple query."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return True


def _check_redis() -> bool:
    """Return whether Redis responds to ping."""
    client = Redis.from_url(
        settings.celery_broker_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        return bool(client.ping())
    finally:
        client.close()


def _check_seatunnel() -> bool:
    """Return whether the SeaTunnel REST API is reachable."""
    endpoint = f"{settings.seatunnel_base_url}/hazelcast/rest/maps/running-jobs"
    response = httpx.get(endpoint, timeout=2)
    response.raise_for_status()
    return True


@app.get("/health")
def health_check() -> JSONResponse:
    """Return service health and dependency readiness status."""
    checks = {
        "database": "ok",
        "redis": "ok",
        "seatunnel": "ok",
    }

    for name, check in (
        ("database", _check_database),
        ("redis", _check_redis),
        ("seatunnel", _check_seatunnel),
    ):
        try:
            check()
        except Exception:
            logger.exception("%s health check failed", name)
            checks[name] = "error"

    status = "ok" if set(checks.values()) == {"ok"} else "degraded"
    return JSONResponse(
        status_code=200 if status == "ok" else 503,
        content={
            "status": status,
            "env": settings.env,
            "version": "0.1.0",
            "checks": checks,
        },
    )


@app.get("/", include_in_schema=False)
def index_page() -> FileResponse:
    """Serve the ETL-Agent console UI."""
    return FileResponse(
        UI_DIR / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/manual", include_in_schema=False)
def operation_manual_page() -> FileResponse:
    """Serve the beginner-friendly operation manual on its own URL."""
    return FileResponse(
        UI_DIR / "docs.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/redoc", include_in_schema=False)
def interface_documentation_page() -> FileResponse:
    """Serve the Chinese interface documentation (ReDoc) page."""
    return FileResponse(
        UI_DIR / "redoc.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/docs", include_in_schema=False)
def legacy_docs_redirect() -> RedirectResponse:
    """Keep the old /docs URL working while moving to /manual."""
    return RedirectResponse(
        url="/manual?v=33",
        status_code=307,
        headers={"Cache-Control": "no-store"},
    )


class NoCacheStaticFiles(StaticFiles):
    """Serve static assets with ``no-cache`` so the browser always revalidates.

    Files under ``/static`` used to be served without ``Cache-Control``. Browsers
    then applied heuristic freshness and kept executing an old ``app.js`` under
    the same ``?v=`` URL, which made shipped frontend fixes look ignored.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


app.mount(
    "/static",
    NoCacheStaticFiles(directory=BASE_DIR / "static"),
    name="static",
)
