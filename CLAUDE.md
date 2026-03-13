# TripWire — Programmable Onchain Event Triggers for AI Agents

## What This Is
TripWire is a programmable onchain event trigger platform for AI agents — the infrastructure layer between onchain events and application execution. x402 payment webhooks are the first use case. Two modes: Notify (Supabase Realtime push) and Execute (Convoy webhook delivery).

## Final Architecture Stack
- **Runtime**: Python 3.11+
- **API**: FastAPI + Uvicorn
- **Database**: Supabase (managed PostgreSQL)
- **Notify Mode**: Supabase Realtime (clients subscribe to DB changes)
- **Webhook Delivery**: Convoy self-hosted + direct httpx fast path
- **Blockchain Indexing**: Goldsky Turbo → delivers events via webhook to TripWire's ingest endpoint
- **Blockchain RPC**: httpx (raw JSON-RPC calls, no web3.py)
- **ABI Decoding**: eth-abi (lightweight, only if needed for raw data)
- **Validation**: Pydantic v2
- **Logging**: structlog
- **HTTP Client**: httpx (async)

## Architecture Layers
- L0 Chain: Base / Ethereum / Arbitrum (ERC-3009 transfers)
- L1 Indexing: Goldsky Turbo → delivers events via webhook to TripWire's /ingest endpoint
- L2 Middleware: TripWire FastAPI (verification, deduplication, identity, policy engine)
- L3 Delivery: Convoy + direct POST (webhook delivery with retries, HMAC signing, DLQ)
- L4 Application: Developer's API (executes business logic on verified webhook)
- L5 MCP: Agent interface (MCP tools for trigger management, middleware registration)

## Key Directories
- `tripwire/ingestion/` — Goldsky pipeline config, ERC-3009 event processing, finality tracking
- `tripwire/api/` — FastAPI routes, endpoint registration, subscription management
- `tripwire/webhook/` — Convoy integration, webhook dispatch
- `tripwire/identity/` — ERC-8004 identity resolution (mock for MVP), reputation scoring
- `tripwire/db/` — Supabase client, repositories, SQL migrations
- `tripwire/types/` — Shared Pydantic models
- `tripwire/config/` — Settings via pydantic-settings
- `tripwire/mcp/` — MCP server, tool handlers, agent middleware registration
- `sdk/` — tripwire-sdk Python package
- `tests/` — Unit and integration tests

## Key Protocols
- **x402**: HTTP 402 micropayment protocol using ERC-3009 transferWithAuthorization
- **ERC-3009**: transferWithAuthorization standard for gasless USDC transfers
- **ERC-8004**: Onchain AI agent identity registry (went mainnet Jan 29 2026)
- **Trigger Registry**: Dynamic trigger system — create triggers for any EVM event via MCP or API, no deploy needed
- **x402 Bazaar**: Agent service discovery via /.well-known/x402-manifest.json

## Webhook Delivery (Convoy + direct httpx fast path)
- Dual-path architecture: direct httpx POST for low-latency fast path; Convoy for managed delivery with retries, HMAC signing, and DLQ
- Convoy self-hosted via docker-compose (convoy-server + convoy-worker + convoy-postgres + convoy-redis)
- Convoy handles: exponential backoff retries, signature signing, delivery logs, endpoint management
- Direct httpx path: used when latency is critical and at-least-once delivery can be handled by the caller
- TripWire wraps both paths to add: policy evaluation, identity enrichment, event deduplication
- docker-compose services: `convoy-server` (port 5005), `convoy-worker`, `convoy-postgres` (port 5433), `convoy-redis` (port 6380)

## Database (Supabase)
- Tables: endpoints, subscriptions, events, nonces, webhook_deliveries, audit_log
- Nonce deduplication via unique constraint on (chain_id, nonce, authorizer)
- Use supabase-py client with service_role key
- SQL migrations in tripwire/db/migrations/

## Conventions
- Pydantic v2 for all input/output validation
- async/await throughout (FastAPI + httpx)
- All amounts in smallest unit (USDC = 6 decimals)
- structlog for structured JSON logging
- No web3.py — use httpx for raw JSON-RPC + eth-abi for decoding
- MCP tools follow the Model Context Protocol spec — mounted at /mcp
