"""MCP tool handlers for TripWire agent operations.

Each handler is an async function with signature:
    async def handler(params: dict, agent_address: str, repos: dict) -> dict
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

import structlog
from nanoid import generate as nanoid

from tripwire.db.repositories.endpoints import EndpointRepository
from tripwire.db.repositories.events import EventRepository
from tripwire.db.repositories.triggers import (
    TriggerRepository,
    TriggerTemplateRepository,
)
from tripwire.types.models import EndpointMode, EndpointPolicies

logger = structlog.get_logger(__name__)


# ── Helpers ──────────────────────────────────────────────────


def _repos(repos: dict) -> tuple[
    EndpointRepository,
    TriggerRepository,
    TriggerTemplateRepository,
    EventRepository,
]:
    return (
        repos["endpoint_repo"],
        repos["trigger_repo"],
        repos["template_repo"],
        repos["event_repo"],
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── 1. register_middleware ───────────────────────────────────


async def register_middleware(
    params: dict, agent_address: str, repos: dict
) -> dict:
    """Register TripWire as middleware for an agent's API endpoint.

    Creates an endpoint and optionally creates triggers from template slugs
    or custom trigger definitions.
    """
    endpoint_repo, trigger_repo, template_repo, _ = _repos(repos)
    supabase = repos["supabase"]

    url: str = params["url"]
    mode: str = params.get("mode", "execute")
    chains: list[int] = params.get("chains", [8453])
    recipient: str = params.get("recipient", agent_address)
    policies: dict = params.get("policies", {})
    template_slugs: list[str] = params.get("template_slugs", [])
    custom_triggers: list[dict] = params.get("custom_triggers", [])

    # Create the endpoint
    now = _now_iso()
    endpoint_id = nanoid(size=21)
    webhook_secret = secrets.token_hex(32)

    endpoint_row = {
        "id": endpoint_id,
        "url": url,
        "mode": mode,
        "chains": chains,
        "recipient": recipient.lower(),
        "owner_address": agent_address.lower(),
        "policies": EndpointPolicies(**policies).model_dump(),
        "webhook_secret": webhook_secret,
        "active": True,
        "created_at": now,
        "updated_at": now,
    }

    result = supabase.table("endpoints").insert(endpoint_row).execute()
    endpoint_data = result.data[0]

    logger.info(
        "mcp_endpoint_created",
        endpoint_id=endpoint_id,
        agent=agent_address,
        mode=mode,
    )

    # Create triggers from template slugs
    trigger_ids: list[str] = []

    for slug in template_slugs:
        template = template_repo.get_by_slug(slug)
        if template is None:
            logger.warning("mcp_template_not_found", slug=slug)
            continue

        trigger_id = nanoid(size=21)
        trigger_data = {
            "id": trigger_id,
            "owner_address": agent_address.lower(),
            "endpoint_id": endpoint_id,
            "name": template.name,
            "event_signature": template.event_signature,
            "abi": template.abi,
            "contract_address": None,
            "chain_ids": chains or template.default_chains,
            "filter_rules": [f.model_dump() for f in template.default_filters],
            "webhook_event_type": template.webhook_event_type,
            "reputation_threshold": template.reputation_threshold,
            "active": True,
            "created_at": now,
            "updated_at": now,
        }
        trigger_repo.create(trigger_data)
        trigger_ids.append(trigger_id)

        logger.info(
            "mcp_trigger_from_template",
            trigger_id=trigger_id,
            template_slug=slug,
        )

    # Create custom triggers
    for ct in custom_triggers:
        trigger_id = nanoid(size=21)
        trigger_data = {
            "id": trigger_id,
            "owner_address": agent_address.lower(),
            "endpoint_id": endpoint_id,
            "name": ct.get("name"),
            "event_signature": ct["event_signature"],
            "abi": ct.get("abi", []),
            "contract_address": ct.get("contract_address"),
            "chain_ids": ct.get("chain_ids", chains),
            "filter_rules": ct.get("filter_rules", []),
            "webhook_event_type": ct.get(
                "webhook_event_type", "payment.confirmed"
            ),
            "reputation_threshold": ct.get("reputation_threshold", 0.0),
            "active": True,
            "created_at": now,
            "updated_at": now,
        }
        trigger_repo.create(trigger_data)
        trigger_ids.append(trigger_id)

        logger.info(
            "mcp_custom_trigger_created",
            trigger_id=trigger_id,
            event_signature=ct["event_signature"],
        )

    return {
        "endpoint_id": endpoint_id,
        "webhook_secret": webhook_secret,
        "trigger_ids": trigger_ids,
        "mode": mode,
        "url": url,
    }


# ── 2. create_trigger ───────────────────────────────────────


async def create_trigger(
    params: dict, agent_address: str, repos: dict
) -> dict:
    """Create a custom trigger for an existing endpoint."""
    endpoint_repo, trigger_repo, _, _ = _repos(repos)

    endpoint_id: str = params["endpoint_id"]
    endpoint = endpoint_repo.get_by_id(endpoint_id)
    if endpoint is None:
        return {"error": "Endpoint not found", "code": "NOT_FOUND"}
    if endpoint.owner_address.lower() != agent_address.lower():
        return {"error": "Not authorized", "code": "FORBIDDEN"}

    now = _now_iso()
    trigger_id = nanoid(size=21)
    trigger_data = {
        "id": trigger_id,
        "owner_address": agent_address.lower(),
        "endpoint_id": endpoint_id,
        "name": params.get("name"),
        "event_signature": params["event_signature"],
        "abi": params.get("abi", []),
        "contract_address": params.get("contract_address"),
        "chain_ids": params.get("chain_ids", endpoint.chains),
        "filter_rules": params.get("filter_rules", []),
        "webhook_event_type": params.get(
            "webhook_event_type", "payment.confirmed"
        ),
        "reputation_threshold": params.get("reputation_threshold", 0.0),
        "active": True,
        "created_at": now,
        "updated_at": now,
    }

    trigger = trigger_repo.create(trigger_data)
    logger.info(
        "mcp_trigger_created",
        trigger_id=trigger_id,
        agent=agent_address,
        endpoint_id=endpoint_id,
    )

    return {
        "trigger_id": trigger.id,
        "endpoint_id": endpoint_id,
        "event_signature": trigger.event_signature,
        "active": trigger.active,
    }


# ── 3. list_triggers ────────────────────────────────────────


async def list_triggers(
    params: dict, agent_address: str, repos: dict
) -> dict:
    """List all triggers owned by the calling agent."""
    _, trigger_repo, _, _ = _repos(repos)

    triggers = trigger_repo.list_by_owner(agent_address)
    active_only = params.get("active_only", True)

    items = []
    for t in triggers:
        if active_only and not t.active:
            continue
        items.append({
            "id": t.id,
            "name": t.name,
            "endpoint_id": t.endpoint_id,
            "event_signature": t.event_signature,
            "chain_ids": t.chain_ids,
            "active": t.active,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })

    return {"triggers": items, "count": len(items)}


# ── 4. delete_trigger ───────────────────────────────────────


async def delete_trigger(
    params: dict, agent_address: str, repos: dict
) -> dict:
    """Deactivate a trigger (soft delete). Verifies ownership."""
    _, trigger_repo, _, _ = _repos(repos)

    trigger_id: str = params["trigger_id"]
    trigger = trigger_repo.get_by_id(trigger_id)
    if trigger is None:
        return {"error": "Trigger not found", "code": "NOT_FOUND"}
    if trigger.owner_address.lower() != agent_address.lower():
        return {"error": "Not authorized", "code": "FORBIDDEN"}

    deactivated = trigger_repo.deactivate(trigger_id)
    if deactivated is None:
        return {"error": "Failed to deactivate trigger", "code": "INTERNAL"}

    logger.info(
        "mcp_trigger_deleted",
        trigger_id=trigger_id,
        agent=agent_address,
    )

    return {"trigger_id": trigger_id, "active": False}


# ── 5. list_templates ───────────────────────────────────────


async def list_templates(
    params: dict, agent_address: str, repos: dict
) -> dict:
    """Browse available trigger templates from the Bazaar."""
    _, _, template_repo, _ = _repos(repos)

    templates = template_repo.list_public()
    category = params.get("category")

    items = []
    for t in templates:
        if category and t.category != category:
            continue
        items.append({
            "slug": t.slug,
            "name": t.name,
            "description": t.description,
            "category": t.category,
            "event_signature": t.event_signature,
            "default_chains": t.default_chains,
            "parameter_schema": t.parameter_schema,
            "reputation_threshold": t.reputation_threshold,
            "install_count": t.install_count,
        })

    return {"templates": items, "count": len(items)}


# ── 6. activate_template ────────────────────────────────────


async def activate_template(
    params: dict, agent_address: str, repos: dict
) -> dict:
    """Instantiate a template with custom params for an endpoint."""
    endpoint_repo, trigger_repo, template_repo, _ = _repos(repos)

    slug: str = params["slug"]
    endpoint_id: str = params["endpoint_id"]
    custom_params: dict = params.get("params", {})

    template = template_repo.get_by_slug(slug)
    if template is None:
        return {"error": "Template not found", "code": "NOT_FOUND"}

    endpoint = endpoint_repo.get_by_id(endpoint_id)
    if endpoint is None:
        return {"error": "Endpoint not found", "code": "NOT_FOUND"}
    if endpoint.owner_address.lower() != agent_address.lower():
        return {"error": "Not authorized", "code": "FORBIDDEN"}

    # Merge default filters with custom params
    filter_rules = [f.model_dump() for f in template.default_filters]
    if "filter_rules" in custom_params:
        filter_rules = custom_params["filter_rules"]

    chain_ids = custom_params.get("chain_ids", template.default_chains)
    contract_address = custom_params.get("contract_address")

    now = _now_iso()
    trigger_id = nanoid(size=21)
    trigger_data = {
        "id": trigger_id,
        "owner_address": agent_address.lower(),
        "endpoint_id": endpoint_id,
        "name": f"{template.name} (from template)",
        "event_signature": template.event_signature,
        "abi": template.abi,
        "contract_address": contract_address,
        "chain_ids": chain_ids,
        "filter_rules": filter_rules,
        "webhook_event_type": template.webhook_event_type,
        "reputation_threshold": template.reputation_threshold,
        "active": True,
        "created_at": now,
        "updated_at": now,
    }

    trigger = trigger_repo.create(trigger_data)

    # Increment install_count on the template (best-effort)
    try:
        repos["supabase"].rpc(
            "increment_install_count",
            {"template_id": template.id},
        ).execute()
    except Exception:
        logger.warning(
            "mcp_template_install_count_update_failed",
            template_id=template.id,
        )

    logger.info(
        "mcp_template_activated",
        trigger_id=trigger_id,
        template_slug=slug,
        endpoint_id=endpoint_id,
        agent=agent_address,
    )

    return {
        "trigger_id": trigger.id,
        "template_slug": slug,
        "endpoint_id": endpoint_id,
        "event_signature": trigger.event_signature,
        "active": True,
    }


# ── 7. get_trigger_status ───────────────────────────────────


async def get_trigger_status(
    params: dict, agent_address: str, repos: dict
) -> dict:
    """Check trigger health and recent event count."""
    _, trigger_repo, _, event_repo = _repos(repos)
    supabase = repos["supabase"]

    trigger_id: str = params["trigger_id"]
    trigger = trigger_repo.get_by_id(trigger_id)
    if trigger is None:
        return {"error": "Trigger not found", "code": "NOT_FOUND"}
    if trigger.owner_address.lower() != agent_address.lower():
        return {"error": "Not authorized", "code": "FORBIDDEN"}

    # Count recent events matching this trigger's endpoint
    try:
        result = (
            supabase.table("events")
            .select("id", count="exact")
            .eq("endpoint_id", trigger.endpoint_id)
            .execute()
        )
        event_count = result.count if result.count is not None else len(result.data)
    except Exception:
        logger.warning("mcp_event_count_failed", trigger_id=trigger_id)
        event_count = -1

    return {
        "trigger_id": trigger.id,
        "name": trigger.name,
        "event_signature": trigger.event_signature,
        "chain_ids": trigger.chain_ids,
        "active": trigger.active,
        "event_count": event_count,
        "created_at": trigger.created_at.isoformat() if trigger.created_at else None,
    }


# ── 8. search_events ────────────────────────────────────────


async def search_events(
    params: dict, agent_address: str, repos: dict
) -> dict:
    """Query recent events for the agent's endpoints."""
    endpoint_repo = repos["endpoint_repo"]
    supabase = repos["supabase"]

    # Get all endpoints owned by the agent
    result = (
        supabase.table("endpoints")
        .select("id")
        .eq("owner_address", agent_address.lower())
        .eq("active", True)
        .execute()
    )
    endpoint_ids = [row["id"] for row in result.data]

    if not endpoint_ids:
        return {"events": [], "count": 0}

    # Build query
    limit = min(params.get("limit", 50), 100)
    status_filter = params.get("status")
    chain_id_filter = params.get("chain_id")

    query = (
        supabase.table("events")
        .select("*")
        .in_("endpoint_id", endpoint_ids)
        .order("created_at", desc=True)
        .limit(limit)
    )

    if status_filter:
        query = query.eq("status", status_filter)
    if chain_id_filter:
        query = query.eq("chain_id", chain_id_filter)

    events_result = query.execute()

    events = []
    for e in events_result.data:
        events.append({
            "id": e.get("id"),
            "endpoint_id": e.get("endpoint_id"),
            "tx_hash": e.get("tx_hash"),
            "chain_id": e.get("chain_id"),
            "status": e.get("status"),
            "block_number": e.get("block_number"),
            "created_at": e.get("created_at"),
        })

    return {"events": events, "count": len(events)}
