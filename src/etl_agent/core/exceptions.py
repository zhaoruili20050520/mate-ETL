"""Domain-specific exceptions used across services and routes.

These are caught by a FastAPI exception handler (or converted to
HTTPException inline) so that service functions stay framework-agnostic.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain-level errors."""

    status_code: int = 400
    detail: str = "Domain error"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.detail)
        self.detail = detail or self.detail


class NotFoundError(DomainError):
    """Raised when a requested resource does not exist."""

    status_code = 404
    detail = "Resource not found"


class PermissionDeniedError(DomainError):
    """Raised when the user lacks the required role for an operation."""

    status_code = 403
    detail = "Permission denied"


class ConflictError(DomainError):
    """Raised when an operation violates a uniqueness constraint."""

    status_code = 409
    detail = "Resource already exists"


class InvalidInputError(DomainError):
    """Raised when the request contains an invalid value."""

    status_code = 422
    detail = "Invalid input"


__all__ = [
    "ConflictError",
    "DomainError",
    "InvalidInputError",
    "NotFoundError",
    "PermissionDeniedError",
]
