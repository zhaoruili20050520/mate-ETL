"""Common Pydantic schemas shared across all endpoints.

This module provides:

- ``PageResponse`` -- a generic paginated wrapper for list endpoints.
  Frontend receives ``{items, total, page, page_size}`` instead of
  a bare ``[...]``, enabling pagination UI without guessing length.
- ``ErrorResponse`` -- a uniform error body.
  All errors (domain, validation, auth, 500) are transformed into
  ``{error: {code, message, details}}`` by the global handlers.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class PageResponse(BaseModel, Generic[T]):
    """Paginated list response.

    Attributes:
        items: the list of items on the current page.
        total: total item count across all pages.
        page: current page number (1-based).
        page_size: items per page.
    """

    items: list[T]
    total: int
    page: int
    page_size: int


class ErrorDetail(BaseModel):
    """Structured error response body."""

    code: str
    message: str
    details: dict[str, Any] | list[Any] | None = None


class ErrorResponse(BaseModel):
    """Top-level error wrapper.

    All API errors return ``{"error": {"code": "...", "message": "...", "details": ...}}``.
    """

    error: ErrorDetail


__all__ = [
    "ErrorDetail",
    "ErrorResponse",
    "PageResponse",
]
