"""Optional Sentry error tracking for TripWire.

All Sentry functionality is wrapped in try/except ImportError so the app works
perfectly without sentry-sdk installed.  Install the optional dependency with:

    pip install tripwire[sentry]
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_sentry_available = False

try:
    import sentry_sdk

    _sentry_available = True
except ImportError:
    sentry_sdk = None  # type: ignore[assignment]


def _strip_secret_str(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """before_send hook that removes any pydantic SecretStr values from event data."""
    try:
        from pydantic import SecretStr
    except ImportError:
        return event

    def _scrub(obj: Any) -> Any:
        if isinstance(obj, SecretStr):
            return "**********"
        if isinstance(obj, dict):
            return {k: _scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(_scrub(item) for item in obj)
        return obj

    return _scrub(event)


def setup_sentry(
    dsn: str,
    environment: str = "production",
    version: str = "",
    traces_sample_rate: float = 0.1,
) -> bool:
    """Initialise Sentry SDK with FastAPI integration.

    Returns True if Sentry was successfully initialised, False otherwise.
    """
    if not _sentry_available:
        logger.info("sentry-sdk not installed; skipping Sentry initialisation")
        return False

    if not dsn:
        logger.info("No Sentry DSN provided; skipping Sentry initialisation")
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=f"tripwire@{version}" if version else None,
            send_default_pii=False,
            traces_sample_rate=traces_sample_rate,
            profiles_sample_rate=0.1,
            before_send=_strip_secret_str,
        )

        sentry_sdk.set_tag("service", "tripwire")
        sentry_sdk.set_tag("env", environment)

        logger.info("Sentry initialised (environment=%s)", environment)
        return True
    except Exception:
        logger.exception("Failed to initialise Sentry")
        return False


def capture_exception(exc: BaseException | None = None) -> None:
    """Report an exception to Sentry.  No-ops if Sentry is not installed."""
    if not _sentry_available:
        return
    try:
        sentry_sdk.capture_exception(exc)
    except Exception:
        logger.debug("Failed to capture exception in Sentry", exc_info=True)


def set_context(key: str, value: dict[str, Any]) -> None:
    """Attach structured context to the current Sentry scope.  No-ops if Sentry is not installed."""
    if not _sentry_available:
        return
    try:
        sentry_sdk.set_context(key, value)
    except Exception:
        logger.debug("Failed to set Sentry context", exc_info=True)
