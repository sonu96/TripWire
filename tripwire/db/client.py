"""Supabase client initialization."""

import structlog
from supabase import Client, create_client

from tripwire.config.settings import settings

logger = structlog.get_logger(__name__)

_client: Client | None = None


def get_supabase_client() -> Client:
    """Return a singleton Supabase client using the service_role key."""
    global _client
    if _client is None:
        _client = create_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )
        logger.info("supabase_client_initialized", url=settings.supabase_url)
    return _client
