"""Trigger and TriggerTemplate CRUD repositories."""

import time
from typing import Any

import structlog
from supabase import Client

from tripwire.types.models import Trigger, TriggerTemplate

logger = structlog.get_logger(__name__)

# ── Module-level caches ──────────────────────────────────────

_active_triggers_cache: list[Trigger] | None = None
_topic_cache: dict[str, tuple[float, list[Trigger]]] = {}
_public_templates_cache: list[TriggerTemplate] | None = None

_TOPIC_CACHE_TTL = 30  # seconds


def invalidate_trigger_cache() -> None:
    """Clear all module-level trigger caches."""
    global _active_triggers_cache, _topic_cache, _public_templates_cache
    _active_triggers_cache = None
    _topic_cache = {}
    _public_templates_cache = None
    logger.info("trigger_cache_invalidated")


class TriggerRepository:
    """CRUD operations for the triggers table."""

    def __init__(self, client: Client) -> None:
        self._sb = client

    def get_by_id(self, trigger_id: str) -> Trigger | None:
        """Fetch a single trigger by ID, or None if not found."""
        result = self._sb.table("triggers").select("*").eq("id", trigger_id).execute()
        if not result.data:
            return None
        return Trigger(**result.data[0])

    def list_active(self) -> list[Trigger]:
        """Return all active triggers (cached)."""
        global _active_triggers_cache
        if _active_triggers_cache is not None:
            return _active_triggers_cache
        result = (
            self._sb.table("triggers")
            .select("*")
            .eq("active", True)
            .order("created_at", desc=True)
            .execute()
        )
        _active_triggers_cache = [Trigger(**row) for row in result.data]
        return _active_triggers_cache

    def find_by_topic(self, topic: str) -> list[Trigger]:
        """Return active triggers matching a topic0 hash (cached, TTL 30s).

        Queries the precomputed ``topic0`` column (keccak256 hash) instead of
        the human-readable ``event_signature``.
        """
        topic_lower = topic.lower()
        now = time.monotonic()
        cached = _topic_cache.get(topic_lower)
        if cached is not None:
            ts, triggers = cached
            if now - ts < _TOPIC_CACHE_TTL:
                return triggers
        result = (
            self._sb.table("triggers")
            .select("*")
            .eq("topic0", topic_lower)
            .eq("active", True)
            .execute()
        )
        triggers = [Trigger(**row) for row in result.data]
        _topic_cache[topic_lower] = (now, triggers)
        return triggers

    def create(self, data: dict[str, Any]) -> Trigger:
        """Insert a new trigger and invalidate caches.

        Automatically computes and stores the ``topic0`` keccak256 hash from
        the ``event_signature`` if not already present.
        """
        if "topic0" not in data and "event_signature" in data:
            from tripwire.utils.topic import compute_topic0

            data["topic0"] = compute_topic0(data["event_signature"])
        result = self._sb.table("triggers").insert(data).execute()
        invalidate_trigger_cache()
        return Trigger(**result.data[0])

    def deactivate(self, trigger_id: str) -> Trigger | None:
        """Soft-delete a trigger by setting active=False."""
        result = (
            self._sb.table("triggers")
            .update({"active": False})
            .eq("id", trigger_id)
            .execute()
        )
        if not result.data:
            return None
        invalidate_trigger_cache()
        return Trigger(**result.data[0])

    def list_by_owner(self, owner_address: str) -> list[Trigger]:
        """Return all triggers for a given owner address."""
        result = (
            self._sb.table("triggers")
            .select("*")
            .eq("owner_address", owner_address.lower())
            .order("created_at", desc=True)
            .execute()
        )
        return [Trigger(**row) for row in result.data]


class TriggerTemplateRepository:
    """CRUD operations for the trigger_templates table."""

    def __init__(self, client: Client) -> None:
        self._sb = client

    def list_public(self) -> list[TriggerTemplate]:
        """Return all public templates (cached)."""
        global _public_templates_cache
        if _public_templates_cache is not None:
            return _public_templates_cache
        result = (
            self._sb.table("trigger_templates")
            .select("*")
            .eq("is_public", True)
            .order("install_count", desc=True)
            .execute()
        )
        _public_templates_cache = [TriggerTemplate(**row) for row in result.data]
        return _public_templates_cache

    def get_by_slug(self, slug: str) -> TriggerTemplate | None:
        """Fetch a single template by slug, or None if not found."""
        result = (
            self._sb.table("trigger_templates")
            .select("*")
            .eq("slug", slug)
            .execute()
        )
        if not result.data:
            return None
        return TriggerTemplate(**result.data[0])
