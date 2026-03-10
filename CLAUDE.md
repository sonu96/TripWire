# TripWire — x402 Execution Middleware

## What This Is
TripWire is "Stripe Webhooks for x402" — the infrastructure layer between x402 micropayments settling onchain and applications executing in response. Two modes: Notify (Supabase Realtime push) and Execute (Svix webhook delivery).

## Final Architecture Stack
- **Runtime**: Python 3.11+
- **API**: FastAPI + Uvicorn
- **Database**: Supabase (managed PostgreSQL)
- **Notify Mode**: Supabase Realtime (clients subscribe to DB changes)
- **Webhook Delivery**: Svix (hosted, free tier 50k msgs/mo — handles retries, HMAC, DLQ)
- **Blockchain Indexing**: Goldsky Mirror/Turbo → pipes directly into Supabase
- **Blockchain RPC**: httpx (raw JSON-RPC calls, no web3.py)
- **ABI Decoding**: eth-abi (lightweight, only if needed for raw data)
- **Validation**: Pydantic v2
- **Logging**: structlog
- **HTTP Client**: httpx (async)

## Architecture Layers
- L0 Chain: Base / Ethereum / Arbitrum (ERC-3009 transfers)
- L1 Indexing: Goldsky Mirror/Turbo → streams events directly into Supabase tables
- L2 Middleware: TripWire FastAPI (verification, deduplication, identity, policy engine)
- L3 Delivery: Svix (webhook delivery with retries, HMAC signing, DLQ)
- L4 Application: Developer's API (executes business logic on verified webhook)

## Key Directories
- `tripwire/ingestion/` — Goldsky pipeline config, ERC-3009 event processing, finality tracking
- `tripwire/api/` — FastAPI routes, endpoint registration, subscription management
- `tripwire/webhook/` — Svix integration, webhook dispatch
- `tripwire/identity/` — ERC-8004 identity resolution (mock for MVP), reputation scoring
- `tripwire/db/` — Supabase client, repositories, SQL migrations
- `tripwire/types/` — Shared Pydantic models
- `tripwire/config/` — Settings via pydantic-settings
- `sdk/` — tripwire-sdk Python package
- `tests/` — Unit and integration tests

## Key Protocols
- **x402**: HTTP 402 micropayment protocol using ERC-3009 transferWithAuthorization
- **ERC-3009**: transferWithAuthorization standard for gasless USDC transfers
- **ERC-8004**: Onchain AI agent identity registry (went mainnet Jan 29 2026)

## Webhook Delivery (Svix)
- One API call to Svix = delivery + retries + HMAC + DLQ
- Svix handles: exponential backoff retries, signature signing, delivery logs, endpoint management
- TripWire wraps Svix to add: policy evaluation, identity enrichment, event deduplication

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
