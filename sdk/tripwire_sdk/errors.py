"""Error hierarchy for the TripWire SDK."""

from __future__ import annotations


class TripWireError(Exception):
    """Base exception for all TripWire SDK errors."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"TripWire API error {status_code}: {detail}")


class TripWireAuthError(TripWireError):
    """Raised on 401/403 responses (authentication / authorization failure)."""


class TripWireNotFoundError(TripWireError):
    """Raised on 404 responses."""


class TripWireRateLimitError(TripWireError):
    """Raised on 429 responses (rate limit exceeded)."""

    def __init__(
        self, status_code: int, detail: str, retry_after: float | None = None
    ) -> None:
        super().__init__(status_code, detail)
        self.retry_after = retry_after


class TripWireServerError(TripWireError):
    """Raised on 5xx responses."""


class TripWireValidationError(TripWireError):
    """Raised when a response cannot be parsed into the expected model."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=0, detail=detail)


class SessionError(TripWireError):
    """Session operation failed (expired, budget exhausted, not found)."""

    def __init__(self, status_code: int, detail: str, session_id: str | None = None) -> None:
        super().__init__(status_code, detail)
        self.session_id = session_id


class SessionExpiredError(SessionError):
    """Session has expired."""
    pass


class BudgetExhaustedError(SessionError):
    """Session budget is exhausted."""
    pass
