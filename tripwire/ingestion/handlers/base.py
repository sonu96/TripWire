"""Base protocol for product-specific event handlers."""

from __future__ import annotations

from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from tripwire.ingestion.processor import EventProcessor


class EventHandler(Protocol):
    """Protocol for product-specific event handlers.

    Each handler declares which event types it can process and
    implements the full handling logic for those events.  The
    ``EventProcessor`` iterates over registered handlers in order
    and delegates to the first one whose ``can_handle`` returns True.
    """

    async def can_handle(
        self,
        event_type: str | tuple[str, list],
        raw_log: dict[str, Any],
    ) -> bool:
        """Return True if this handler can process the event.

        *event_type* is the result of ``EventProcessor._detect_event_type``:
        either a plain string (e.g. ``"erc3009_transfer"``) or a tuple of
        ``("dynamic", triggers)``.
        """
        ...

    async def handle(
        self,
        raw_log: dict[str, Any],
        processor: EventProcessor,
        event_type: str | tuple[str, list],
    ) -> dict[str, Any] | None:
        """Process the event.  Return a result dict or None."""
        ...
