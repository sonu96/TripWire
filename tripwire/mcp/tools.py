"""MCP tool handlers for TripWire agent operations.

Each handler is an async function with signature:
    async def handler(params: dict, ctx: MCPAuthContext, repos: dict) -> dict
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from fastapi import HTTPException
from nanoid import generate as nanoid

from tripwire.config.settings import settings
from tripwire.mcp.types import MCPAuthContext
from tripwire.mcp.protocols import KNOWN_POOLS

from tripwire.db.repositories.endpoints import EndpointRepository
from tripwire.db.repositories.events import EventRepository
from tripwire.db.repositories.triggers import (
    TriggerRepository,
    TriggerTemplateRepository,
)
from tripwire.types.models import EndpointMode, EndpointPolicies, execution_state_from_status
from tripwire.utils.topic import compute_topic0
from tripwire.db.repositories.quotas import check_endpoint_quota, check_trigger_quota
from tripwire.cache import RedisCache
from tripwire.api.redis import get_redis

logger = structlog.get_logger(__name__)


def _get_shared_cache(prefix: str) -> RedisCache:
    """Return a RedisCache instance for the given prefix."""
    return RedisCache(get_redis(), prefix=prefix, default_ttl=30)


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


# ── Shared endpoint-creation logic ───────────────────────────


async def _create_endpoint_core(
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """Core endpoint-creation logic shared by register_endpoint and register_middleware.

    Creates the endpoint row and returns a dict with endpoint_id, webhook_secret,
    mode, and url.  Returns an ``{"error": ...}`` dict on failure.
    """
    from tripwire.api.validation import validate_endpoint_url

    supabase = repos["supabase"]

    url: str = params["url"]
    mode: str = params.get("mode", "execute")
    chains: list[int] = params.get("chains", [8453])
    recipient: str = params.get("recipient", ctx.agent_address)
    policies: dict = params.get("policies", {})

    # SSRF protection: block localhost, loopback, private IPs
    try:
        validate_endpoint_url(url)
    except ValueError as exc:
        return {"error": str(exc), "code": "INVALID_URL"}

    # Quota check before creating endpoint
    await check_endpoint_quota(supabase, ctx.agent_address)

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
        "owner_address": ctx.agent_address.lower(),
        "policies": EndpointPolicies(**policies).model_dump(),
        "active": True,
        "created_at": now,
        "updated_at": now,
    }

    result = supabase.table("endpoints").insert(endpoint_row).execute()
    endpoint_data = result.data[0]

    logger.info(
        "mcp_endpoint_created",
        endpoint_id=endpoint_id,
        agent=ctx.agent_address,
        mode=mode,
    )

    # Wire up webhook provider (Convoy) for execute-mode endpoints
    convoy_project_id = None
    convoy_endpoint_id = None
    if mode == "execute":
        provider = repos.get("webhook_provider")
        if provider is not None:
            try:
                convoy_project_id = await provider.create_app(
                    developer_id=endpoint_id,
                    name=f"tripwire-{endpoint_id}",
                )
                convoy_endpoint_id = await provider.create_endpoint(
                    app_id=convoy_project_id,
                    url=url,
                    description=f"TripWire endpoint for {recipient}",
                    secret=webhook_secret,
                )
                # Persist the provider IDs back to the endpoint row
                supabase.table("endpoints").update({
                    "convoy_project_id": convoy_project_id,
                    "convoy_endpoint_id": convoy_endpoint_id,
                }).eq("id", endpoint_id).execute()
                logger.info(
                    "mcp_webhook_provider_wired",
                    endpoint_id=endpoint_id,
                    convoy_project_id=convoy_project_id,
                    convoy_endpoint_id=convoy_endpoint_id,
                )
            except Exception:
                logger.exception(
                    "mcp_webhook_provider_setup_failed",
                    endpoint_id=endpoint_id,
                )
                # Clean up the endpoint row — can't deliver webhooks without Convoy
                supabase.table("endpoints").delete().eq("id", endpoint_id).execute()
                return {
                    "error": "Webhook provider setup failed. Endpoint was not created.",
                    "code": "WEBHOOK_SETUP_FAILED",
                }
        else:
            logger.warning(
                "mcp_no_webhook_provider",
                endpoint_id=endpoint_id,
                mode=mode,
            )

    return {
        "endpoint_id": endpoint_id,
        "webhook_secret": webhook_secret,
        "mode": mode,
        "url": url,
        "chains": chains,
    }


# ── 1a. register_endpoint ───────────────────────────────────


async def register_endpoint(
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """Create a webhook endpoint. Returns endpoint_id and webhook_secret."""
    core_result = await _create_endpoint_core(params, ctx, repos)
    if "error" in core_result:
        return core_result

    return {
        "endpoint_id": core_result["endpoint_id"],
        "webhook_secret": core_result["webhook_secret"],
        "mode": core_result["mode"],
        "url": core_result["url"],
    }


# ── 1b. register_middleware (deprecated) ─────────────────────


async def register_middleware(
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """(Deprecated) Register TripWire as middleware — creates endpoint + triggers."""
    core_result = await _create_endpoint_core(params, ctx, repos)
    if "error" in core_result:
        return core_result

    supabase = repos["supabase"]
    _, trigger_repo, template_repo, _ = _repos(repos)

    endpoint_id = core_result["endpoint_id"]
    chains = core_result["chains"]
    now = _now_iso()

    template_slugs: list[str] = params.get("template_slugs", [])
    custom_triggers: list[dict] = params.get("custom_triggers", [])

    # Create triggers from template slugs
    trigger_ids: list[str] = []

    try:
        for slug in template_slugs:
            # Re-check quota before each insert to tighten the race window
            await check_trigger_quota(supabase, ctx.agent_address)

            template = template_repo.get_by_slug(slug)
            if template is None:
                logger.warning("mcp_template_not_found", slug=slug)
                continue

            trigger_id = nanoid(size=21)
            trigger_data = {
                "id": trigger_id,
                "owner_address": ctx.agent_address.lower(),
                "endpoint_id": endpoint_id,
                "name": template.name,
                "event_signature": template.event_signature,
                "topic0": compute_topic0(template.event_signature),
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
            # Re-check quota before each insert to tighten the race window
            await check_trigger_quota(supabase, ctx.agent_address)

            trigger_id = nanoid(size=21)
            trigger_data = {
                "id": trigger_id,
                "owner_address": ctx.agent_address.lower(),
                "endpoint_id": endpoint_id,
                "name": ct.get("name"),
                "event_signature": ct["event_signature"],
                "topic0": compute_topic0(ct["event_signature"]),
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
    except HTTPException as exc:
        # Clean up: delete the orphan endpoint on quota or insert failure
        try:
            await supabase.table("endpoints").delete().eq("id", endpoint_id).execute()
        except Exception:
            logger.exception("mcp_orphan_endpoint_cleanup_failed", endpoint_id=endpoint_id)
        raise exc

    # Invalidate trigger cache if triggers were created
    if trigger_ids:
        try:
            trigger_cache = _get_shared_cache("tripwire:triggers")
            await trigger_cache.invalidate_pattern("topic:*")
        except Exception:
            logger.debug("mcp_cache_invalidation_failed")

    return {
        "endpoint_id": endpoint_id,
        "webhook_secret": core_result["webhook_secret"],
        "trigger_ids": trigger_ids,
        "mode": core_result["mode"],
        "url": core_result["url"],
        "_deprecated": True,
        "migration_hint": "Use register_endpoint + create_trigger separately",
    }


# ── 2. create_trigger ───────────────────────────────────────


async def create_trigger(
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """Create a custom trigger for an existing endpoint."""
    endpoint_repo, trigger_repo, _, _ = _repos(repos)
    supabase = repos["supabase"]

    # Quota check before creating trigger
    await check_trigger_quota(supabase, ctx.agent_address)

    endpoint_id: str = params["endpoint_id"]
    endpoint = endpoint_repo.get_by_id(endpoint_id)
    if endpoint is None:
        return {"error": "Endpoint not found", "code": "NOT_FOUND"}
    if endpoint.owner_address.lower() != ctx.agent_address.lower():
        return {"error": "Not authorized", "code": "FORBIDDEN"}

    now = _now_iso()
    trigger_id = nanoid(size=21)
    trigger_data = {
        "id": trigger_id,
        "owner_address": ctx.agent_address.lower(),
        "endpoint_id": endpoint_id,
        "name": params.get("name"),
        "event_signature": params["event_signature"],
        "topic0": compute_topic0(params["event_signature"]),
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
        agent=ctx.agent_address,
        endpoint_id=endpoint_id,
    )

    # Invalidate trigger cache
    try:
        cache = _get_shared_cache("tripwire:triggers")
        await cache.invalidate_pattern("topic:*")
    except Exception:
        logger.debug("mcp_cache_invalidation_failed")

    return {
        "trigger_id": trigger.id,
        "endpoint_id": endpoint_id,
        "event_signature": trigger.event_signature,
        "active": trigger.active,
    }


# ── 3. list_triggers ────────────────────────────────────────


async def list_triggers(
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """List all triggers owned by the calling agent."""
    _, trigger_repo, _, _ = _repos(repos)

    triggers = trigger_repo.list_by_owner(ctx.agent_address)
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
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """Deactivate a trigger (soft delete). Verifies ownership."""
    _, trigger_repo, _, _ = _repos(repos)

    trigger_id: str = params["trigger_id"]
    trigger = trigger_repo.get_by_id(trigger_id)
    if trigger is None:
        return {"error": "Trigger not found", "code": "NOT_FOUND"}
    if trigger.owner_address.lower() != ctx.agent_address.lower():
        return {"error": "Not authorized", "code": "FORBIDDEN"}

    deactivated = trigger_repo.deactivate(trigger_id)
    if deactivated is None:
        return {"error": "Failed to deactivate trigger", "code": "INTERNAL"}

    logger.info(
        "mcp_trigger_deleted",
        trigger_id=trigger_id,
        agent=ctx.agent_address,
    )

    # Invalidate trigger cache
    try:
        cache = _get_shared_cache("tripwire:triggers")
        await cache.invalidate_pattern("topic:*")
    except Exception:
        logger.debug("mcp_cache_invalidation_failed")

    return {"trigger_id": trigger_id, "active": False}


# ── 5. list_templates ───────────────────────────────────────


async def list_templates(
    params: dict, ctx: MCPAuthContext, repos: dict
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
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """Instantiate a template with custom params for an endpoint."""
    endpoint_repo, trigger_repo, template_repo, _ = _repos(repos)
    supabase = repos["supabase"]

    # Quota check before creating trigger from template
    await check_trigger_quota(supabase, ctx.agent_address)

    slug: str = params["slug"]
    endpoint_id: str = params["endpoint_id"]
    custom_params: dict = params.get("params", {})

    template = template_repo.get_by_slug(slug)
    if template is None:
        return {"error": "Template not found", "code": "NOT_FOUND"}

    endpoint = endpoint_repo.get_by_id(endpoint_id)
    if endpoint is None:
        return {"error": "Endpoint not found", "code": "NOT_FOUND"}
    if endpoint.owner_address.lower() != ctx.agent_address.lower():
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
        "owner_address": ctx.agent_address.lower(),
        "endpoint_id": endpoint_id,
        "name": f"{template.name} (from template)",
        "event_signature": template.event_signature,
        "topic0": compute_topic0(template.event_signature),
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
        agent=ctx.agent_address,
    )

    # Invalidate trigger cache
    try:
        cache = _get_shared_cache("tripwire:triggers")
        await cache.invalidate_pattern("topic:*")
    except Exception:
        logger.debug("mcp_cache_invalidation_failed")

    return {
        "trigger_id": trigger.id,
        "template_slug": slug,
        "endpoint_id": endpoint_id,
        "event_signature": trigger.event_signature,
        "active": True,
    }


# ── 7. get_trigger_status ───────────────────────────────────


async def get_trigger_status(
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """Check trigger health and recent event count."""
    _, trigger_repo, _, event_repo = _repos(repos)
    supabase = repos["supabase"]

    trigger_id: str = params["trigger_id"]
    trigger = trigger_repo.get_by_id(trigger_id)
    if trigger is None:
        return {"error": "Trigger not found", "code": "NOT_FOUND"}
    if trigger.owner_address.lower() != ctx.agent_address.lower():
        return {"error": "Not authorized", "code": "FORBIDDEN"}

    # Count recent events matching this trigger.
    # Prefer trigger_id column (added in migration 026) for accurate
    # per-trigger counts; fall back to endpoint_id if the column is
    # unavailable or the query fails.
    try:
        result = (
            supabase.table("events")
            .select("id", count="exact")
            .eq("trigger_id", trigger_id)
            .execute()
        )
        event_count = result.count if result.count is not None else len(result.data)
    except Exception:
        # Fallback: trigger_id column may not exist on older deployments
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

    # Fetch the most recent event's status for execution state.
    # Same fallback strategy: prefer trigger_id, fall back to endpoint_id.
    last_event_execution_state = None
    try:
        last_evt = (
            supabase.table("events")
            .select("status")
            .eq("trigger_id", trigger_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not last_evt.data:
            # Fallback to endpoint_id if no results with trigger_id
            last_evt = (
                supabase.table("events")
                .select("status")
                .eq("endpoint_id", trigger.endpoint_id)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
        if last_evt.data:
            state, _, _ = execution_state_from_status(last_evt.data[0].get("status", "pending"))
            last_event_execution_state = state.value
    except Exception:
        logger.warning("mcp_last_event_status_failed", trigger_id=trigger_id)

    return {
        "trigger_id": trigger.id,
        "name": trigger.name,
        "event_signature": trigger.event_signature,
        "chain_ids": trigger.chain_ids,
        "active": trigger.active,
        "event_count": event_count,
        "last_event_execution_state": last_event_execution_state,
        "created_at": trigger.created_at.isoformat() if trigger.created_at else None,
    }


# ── 8. search_events ────────────────────────────────────────


async def search_events(
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """Query recent events for the agent's endpoints."""
    endpoint_repo = repos["endpoint_repo"]
    supabase = repos["supabase"]

    # Get all endpoints owned by the agent
    result = (
        supabase.table("endpoints")
        .select("id")
        .eq("owner_address", ctx.agent_address.lower())
        .eq("active", True)
        .execute()
    )
    endpoint_ids = [row["id"] for row in result.data]

    if not endpoint_ids:
        return {"events": [], "count": 0}

    # Build query using event_endpoints join table (#7)
    limit = min(params.get("limit", 50), 100)
    status_filter = params.get("status")
    chain_id_filter = params.get("chain_id")

    # Fetch a larger set from the join table (pre-filter) so that
    # status/chain_id filters applied to the events query don't drop
    # older matching events that would otherwise be within the limit.
    prefetch_limit = 500 if (status_filter or chain_id_filter) else limit

    # Get event IDs linked to the agent's endpoints via join table
    ee_result = (
        supabase.table("event_endpoints")
        .select("event_id")
        .in_("endpoint_id", endpoint_ids)
        .order("created_at", desc=True)
        .limit(prefetch_limit)
        .execute()
    )
    event_ids = list({row["event_id"] for row in ee_result.data})

    if not event_ids:
        return {"events": [], "count": 0}

    query = (
        supabase.table("events")
        .select("*")
        .in_("id", event_ids)
    )

    if status_filter:
        query = query.eq("status", status_filter)
    if chain_id_filter:
        query = query.eq("chain_id", chain_id_filter)

    query = query.order("created_at", desc=True).limit(limit)

    events_result = query.execute()

    events = []
    for e in events_result.data:
        state, safe, source = execution_state_from_status(e.get("status", "pending"))
        events.append({
            "id": e.get("id"),
            "tx_hash": e.get("tx_hash"),
            "chain_id": e.get("chain_id"),
            "status": e.get("status"),
            "block_number": e.get("block_number"),
            "created_at": e.get("created_at"),
            "execution_state": state.value,
            "safe_to_execute": safe,
            "trust_source": source.value,
        })

    return {"events": events, "count": len(events)}


# ── 9. fetch_abi ─────────────────────────────────────────────

# Block explorer API URL mapping
_EXPLORER_URLS: dict[str, str] = {
    "base": "https://api.basescan.org/api",
    "ethereum": "https://api.etherscan.io/api",
    "arbitrum": "https://api.arbiscan.io/api",
}

# Chain name → settings attribute for the API key
_EXPLORER_KEY_ATTR: dict[str, str] = {
    "base": "basescan_api_key",
    "ethereum": "etherscan_api_key",
    "arbitrum": "arbiscan_api_key",
}

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _parse_event_signature(abi_item: dict) -> str:
    """Build a Solidity-style event signature from an ABI event entry."""
    name = abi_item.get("name", "")
    inputs = abi_item.get("inputs", [])
    param_types = [inp.get("type", "") for inp in inputs]
    return f"{name}({','.join(param_types)})"


async def fetch_abi(
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """Fetch the verified ABI for a contract and list its events."""
    contract_address: str = params["contract_address"]
    chain: str = params["chain"]

    # Validate contract address format
    if not _ADDRESS_RE.match(contract_address):
        return {
            "error": "Invalid contract address format. Expected 0x followed by 40 hex characters.",
            "code": "INVALID_INPUT",
        }

    # Validate chain
    if chain not in _EXPLORER_URLS:
        return {
            "error": f"Unsupported chain '{chain}'. Supported: {', '.join(_EXPLORER_URLS.keys())}",
            "code": "INVALID_INPUT",
        }

    explorer_url = _EXPLORER_URLS[chain]
    api_key = getattr(settings, _EXPLORER_KEY_ATTR[chain], "")

    # Build request params
    req_params = {
        "module": "contract",
        "action": "getabi",
        "address": contract_address,
    }
    if api_key:
        req_params["apikey"] = api_key

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            resp = await client.get(explorer_url, params=req_params)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        return {
            "error": "Block explorer API timed out. Try again later.",
            "code": "TIMEOUT",
        }
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            return {
                "error": "Block explorer API rate limit exceeded. Try again in a few seconds.",
                "code": "RATE_LIMITED",
            }
        return {
            "error": f"Block explorer API returned HTTP {exc.response.status_code}.",
            "code": "API_ERROR",
        }
    except Exception as exc:
        logger.warning("fetch_abi_request_failed", error=str(exc), chain=chain)
        return {
            "error": "Failed to reach block explorer API.",
            "code": "API_ERROR",
        }

    # Check if contract is verified
    status = data.get("status")
    message = data.get("message", "")
    result = data.get("result", "")

    if status != "1" or "not verified" in message.lower() or "not verified" in str(result).lower():
        return {
            "contract": contract_address,
            "chain": chain,
            "verified": False,
            "events": [],
            "total_events": 0,
            "error": "Contract source code not verified on block explorer.",
        }

    # Parse ABI JSON
    try:
        abi = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return {
            "error": "Failed to parse ABI from block explorer response.",
            "code": "PARSE_ERROR",
        }

    # Extract events
    events = []
    for item in abi:
        if item.get("type") != "event":
            continue

        name = item.get("name", "")
        inputs = item.get("inputs", [])
        signature = _parse_event_signature(item)

        input_list = []
        filterable_fields = []
        for inp in inputs:
            inp_name = inp.get("name", "")
            inp_type = inp.get("type", "")
            indexed = inp.get("indexed", False)
            input_list.append({
                "name": inp_name,
                "type": inp_type,
                "indexed": indexed,
            })
            if not indexed:
                filterable_fields.append(inp_name)

        events.append({
            "name": name,
            "signature": signature,
            "inputs": input_list,
            "filterable_fields": filterable_fields,
        })

    logger.info(
        "mcp_fetch_abi",
        contract=contract_address,
        chain=chain,
        event_count=len(events),
        agent=ctx.agent_address,
    )

    return {
        "contract": contract_address,
        "chain": chain,
        "verified": True,
        "events": events,
        "total_events": len(events),
    }


# ── 10. list_pools ───────────────────────────────────────────


async def list_pools(
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """List popular pools for a known DeFi protocol."""
    protocol: str = params["protocol"].lower().strip()
    chain: str = params.get("chain", "base").lower().strip()
    limit: int = params.get("limit", 10)

    if protocol not in KNOWN_POOLS:
        supported = ", ".join(sorted(KNOWN_POOLS.keys()))
        return {
            "error": f"Unknown protocol '{protocol}'. Supported protocols: {supported}. "
                     f"Use fetch_abi with a specific contract address to explore unknown contracts.",
            "code": "UNKNOWN_PROTOCOL",
        }

    protocol_data = KNOWN_POOLS[protocol]

    # Check if the protocol is available on the requested chain
    protocol_chain = protocol_data.get("chain", "base")
    if chain != protocol_chain:
        return {
            "error": f"Protocol '{protocol}' is configured for {protocol_chain}, "
                     f"not {chain}. Try chain='{protocol_chain}'.",
            "code": "CHAIN_MISMATCH",
        }

    pools = protocol_data["pools"][:limit]

    logger.info(
        "mcp_list_pools",
        protocol=protocol,
        chain=chain,
        pool_count=len(pools),
        agent=ctx.agent_address,
    )

    return {
        "protocol": protocol,
        "chain": chain,
        "type": protocol_data.get("type", ""),
        "pools": pools,
        "available_events": protocol_data.get("events", []),
        "hint": "Use fetch_abi with the pool address to get the full event ABI, then create_trigger to set up a webhook.",
    }


# ── 11. validate_trigger ─────────────────────────────────────

# Supported chains
_SUPPORTED_CHAINS: dict[int, str] = {
    8453: "Base",
    1: "Ethereum",
    42161: "Arbitrum",
}

# Valid filter operators (sourced from tripwire/ingestion/filter_engine.py)
_VALID_OPS: set[str] = {
    "eq", "neq", "gt", "gte", "lt", "lte",
    "in", "not_in", "between", "not_between",
    "contains", "regex", "jmespath",
}

# Regex to validate event signature format: Name(type,type,...)
_EVENT_SIG_RE = re.compile(
    r"^[A-Z][a-zA-Z0-9_]*\("
    r"(?:[a-zA-Z0-9\[\]_]+(?:\s+(?:indexed\s+)?[a-zA-Z0-9_]*)?"
    r"(?:,[a-zA-Z0-9\[\]_]+(?:\s+(?:indexed\s+)?[a-zA-Z0-9_]*)?)*)?"
    r"\)$"
)

# Simpler regex for just extracting types from a signature like Swap(address,address,int256)
_SIG_TYPES_RE = re.compile(r"^([A-Z][a-zA-Z0-9_]*)\(([^)]*)\)$")


def _parse_sig_fields(event_signature: str) -> tuple[str | None, list[str]]:
    """Parse event name and parameter types from a signature string.

    Returns (event_name, list_of_types) or (None, []) on parse failure.
    """
    m = _SIG_TYPES_RE.match(event_signature)
    if not m:
        return None, []
    name = m.group(1)
    params_str = m.group(2).strip()
    if not params_str:
        return name, []
    types = [t.strip() for t in params_str.split(",") if t.strip()]
    return name, types


async def validate_trigger(
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """Validate a trigger configuration before deploying it."""
    event_signature: str = params["event_signature"]
    contract_address: str = params["contract_address"]
    chain_id: int = params["chain_id"]
    filter_rules: list[dict] = params.get("filter_rules", [])

    errors: list[str] = []

    # 1. Validate event signature format
    event_name, param_types = _parse_sig_fields(event_signature)
    if event_name is None:
        errors.append(
            f"Invalid event signature format: '{event_signature}'. "
            f"Expected format: EventName(type1,type2,...) e.g. Swap(address,address,int256,int256,uint160,uint128,int24)"
        )

    # 2. Validate contract address
    if not _ADDRESS_RE.match(contract_address):
        errors.append(
            f"Invalid contract address: '{contract_address}'. "
            f"Expected 0x followed by 40 hex characters."
        )

    # 3. Validate chain_id
    if chain_id not in _SUPPORTED_CHAINS:
        supported = ", ".join(f"{cid} ({name})" for cid, name in _SUPPORTED_CHAINS.items())
        errors.append(
            f"Unsupported chain_id: {chain_id}. Supported chains: {supported}"
        )

    # 4. Validate filter rules
    # Build positional field names from the event signature (param0, param1, ...)
    # In practice, agents will pass field names from fetch_abi output which have
    # real names; positional names are the fallback when no ABI names are available.
    field_names: list[str] = []
    if event_name is not None:
        field_names = [f"param{i}" for i in range(len(param_types))]

    # If an event_abi is provided in params, extract real field names
    event_abi: list[dict] | None = params.get("event_abi")
    if event_abi and isinstance(event_abi, list):
        field_names = [inp.get("name", f"param{i}") for i, inp in enumerate(event_abi)]

    for i, rule in enumerate(filter_rules):
        field = rule.get("field", "")
        op = rule.get("op", "")
        # value is optional for validation purposes

        # Validate operator
        if op and op not in _VALID_OPS:
            errors.append(
                f"Filter rule {i}: Operator '{op}' not supported. "
                f"Valid operators: {', '.join(sorted(_VALID_OPS))}"
            )

        # Validate field name against known fields (if we have names from ABI)
        if field and field_names and field not in field_names:
            # Also accept JMESPath-style dotted paths (e.g. "args.tick")
            base_field = field.split(".")[0]
            if base_field not in field_names:
                errors.append(
                    f"Filter rule {i}: Field '{field}' not found in event {event_name or 'unknown'}. "
                    f"Available fields: {', '.join(field_names)}"
                )

    # Return result
    if errors:
        logger.info(
            "mcp_validate_trigger_failed",
            errors=errors,
            agent=ctx.agent_address,
        )
        return {
            "valid": False,
            "errors": errors,
        }

    # Build friendly filter summary
    filter_summaries = []
    for rule in filter_rules:
        field = rule.get("field", "?")
        op = rule.get("op", "eq").upper().replace("_", " ")
        value = rule.get("value", "?")
        if rule.get("op") in ("between", "not_between"):
            # Format "low,high" as "low AND high"
            value = str(value).replace(",", " AND ")
        filter_summaries.append(f"{field} {op} {value}")

    chain_name = _SUPPORTED_CHAINS.get(chain_id, f"chain {chain_id}")

    logger.info(
        "mcp_validate_trigger_ok",
        event_name=event_name,
        chain_id=chain_id,
        filter_count=len(filter_rules),
        agent=ctx.agent_address,
    )

    return {
        "valid": True,
        "event_name": event_name,
        "event_fields": field_names if field_names else param_types,
        "filter_summary": "; ".join(filter_summaries) if filter_summaries else "No filters",
        "chain": chain_name,
        "hint": "Trigger is valid. Call create_trigger to deploy it.",
    }
