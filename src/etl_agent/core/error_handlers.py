"""Global exception handlers for uniform error responses.

All API errors are transformed into::

    {"error": {"code": "<string>", "message": "<human-readable>", "details": ...}}

This ensures the frontend has a single error shape to parse,
regardless of whether the error originated from:

- Domain logic (NotFoundError, PermissionDeniedError, etc.)
- FastAPI validation (422)
- Auth failures (401/403)
- Unexpected server errors (500)
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse

from etl_agent.core.exceptions import DomainError

logger = logging.getLogger(__name__)


def _error_body(
    code: str,
    message: str,
    details: dict[str, Any] | list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    """Register all global exception handlers on the FastAPI app."""

    @app.exception_handler(HTTPException)
    async def _http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(
                "http_error",
                str(exc.detail),
            ),
            headers=exc.headers,
        )

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        code = type(exc).__name__.replace("Error", "").lower() or "domain_error"
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(code, exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_error_body(
                "validation_error",
                "Request validation failed",
                details=jsonable_encoder(exc.errors()),
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception in API handler")
        return JSONResponse(
            status_code=500,
            content=_error_body(
                "internal_error",
                "An unexpected error occurred",
                details={"type": type(exc).__name__},
            ),
        )
