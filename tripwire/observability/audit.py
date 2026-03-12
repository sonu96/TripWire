"""Fire-and-forget audit logging to the Supabase audit_log table."""

import asyncio
import re
from datetime import datetime, timezone

import structlog
from supabase import Client

logger = structlog.get_logger(__name__)

# Ethereum address pattern for actor sanitisation
_ETH_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Known non-wallet system actors that are also valid
_SYSTEM_ACTORS = frozenset({"unknown", "system"})

# Module-level set to prevent fire-and-forget tasks from being GC'd
_background_tasks: set[asyncio.Task] = set()


def fire_and_forget(coro) -> None:
    """Schedule *coro* as a background task without risk of GC before completion."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


class AuditLogger:
    """Writes audit events to the ``audit_log`` Supabase table.

    All writes are fire-and-forget: errors are logged but never propagated so
    that audit logging can never break a user-facing request.
    """

    def __init__(self, supabase: Client) -> None:
        self._sb = supabase

    @staticmethod
    def _sanitize_actor(actor: str) -> str:
        """Return *actor* if it looks like a valid Ethereum address or known
        system identifier, otherwise return ``"invalid_actor"``."""
        if actor in _SYSTEM_ACTORS:
            return actor
        if _ETH_ADDRESS_RE.match(actor):
            return actor
        return "invalid_actor"

    async def log(
        self,
        action: str,
        actor: str,
        resource_type: str,
        resource_id: str,
        details: dict | None = None,
        ip_address: str | None = None,
    ) -> None:
        """Insert a single audit event.

        Parameters
        ----------
        action:
            Dot-delimited action string, e.g. ``"endpoint.created"``.
        actor:
            Identifier of the caller (typically a wallet address).
        resource_type:
            High-level resource category (``"endpoint"``, ``"subscription"``, ``"auth"``).
        resource_id:
            The specific resource identifier (endpoint id, subscription id, etc.).
        details:
            Optional JSON-serialisable dict with extra context.
        ip_address:
            Optional client IP address.
        """
        try:
            row = {
                "action": action,
                "actor": self._sanitize_actor(actor),
                "resource_type": resource_type,
                "resource_id": resource_id,
                "details": details or {},
                "ip_address": ip_address,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            await asyncio.to_thread(
                lambda: self._sb.table("audit_log").insert(row).execute()
            )
        except Exception:
            logger.warning(
                "audit_log_write_failed",
                action=action,
                actor=actor,
                resource_type=resource_type,
                resource_id=resource_id,
                exc_info=True,
            )
