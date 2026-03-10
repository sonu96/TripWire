# Configuration Reference

TripWire is configured via environment variables, loaded from a `.env` file by [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/). Copy `.env.example` to `.env` and fill in your values.

## All Environment Variables

### Application

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `APP_ENV` | No | `development` | Environment name. Set to `production` for production deploys. Controls Uvicorn auto-reload (enabled in development). |
| `APP_PORT` | No | `3402` | Port the FastAPI server listens on. |
| `LOG_LEVEL` | No | `info` | Logging level (`debug`, `info`, `warning`, `error`). Passed to Uvicorn and structlog. |

### Supabase (Database)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SUPABASE_URL` | **Yes** | -- | Your Supabase project URL (e.g. `https://abcdefgh.supabase.co`). Found in **Settings > API**. |
| `SUPABASE_ANON_KEY` | **Yes** | -- | Supabase anon/public key. Used for client-side operations and Realtime subscriptions. |
| `SUPABASE_SERVICE_ROLE_KEY` | **Yes** | -- | Supabase service role key. Used for server-side database operations. **Keep this secret.** |

### Svix (Webhook Delivery)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SVIX_API_KEY` | **Yes** | -- | Svix API key for webhook delivery. Get it from your [Svix dashboard](https://dashboard.svix.com). |
| `SVIX_SIGNING_SECRET` | No | -- | Signing secret for webhook signature verification (format: `whsec_...`). Consumers use this to verify incoming webhooks. |

### Goldsky (Blockchain Indexing)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOLDSKY_API_KEY` | No | `""` (empty) | Goldsky API key for blockchain event indexing. Required for production to stream onchain events into Supabase. |
| `GOLDSKY_PROJECT_ID` | No | `""` (empty) | Goldsky project ID. |

### Blockchain RPC

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BASE_RPC_URL` | No | `https://mainnet.base.org` | Base mainnet JSON-RPC endpoint. Used for finality checks. |
| `ETHEREUM_RPC_URL` | No | `https://eth.llamarpc.com` | Ethereum mainnet JSON-RPC endpoint. Used for finality checks. |
| `ARBITRUM_RPC_URL` | No | `https://arb1.arbitrum.io/rpc` | Arbitrum One JSON-RPC endpoint. Used for finality checks. |

### ERC-8004 (Identity Registry)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ERC8004_IDENTITY_REGISTRY` | No | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` | ERC-8004 Identity Registry contract address. Same on all chains via CREATE2. |
| `ERC8004_REPUTATION_REGISTRY` | No | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` | ERC-8004 Reputation Registry contract address. Same on all chains via CREATE2. |

### USDC Contract Addresses

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `USDC_BASE` | No | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | USDC contract address on Base. |
| `USDC_ETHEREUM` | No | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` | USDC contract address on Ethereum. |
| `USDC_ARBITRUM` | No | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` | USDC contract address on Arbitrum. |

## Minimal Configuration

For local development, you need at minimum these four variables:

```env
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_SERVICE_ROLE_KEY=eyJ...
SVIX_API_KEY=sk_...
```

Everything else has sensible defaults. Goldsky credentials are only needed when you want to index live blockchain events in production.

## Example `.env` File

```env
# App
APP_ENV=development
APP_PORT=3402
LOG_LEVEL=info

# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# Svix
SVIX_API_KEY=sk_your_svix_api_key
SVIX_SIGNING_SECRET=whsec_your_signing_secret

# Goldsky (production only)
GOLDSKY_API_KEY=
GOLDSKY_PROJECT_ID=

# Blockchain RPC (defaults are fine for most use cases)
BASE_RPC_URL=https://mainnet.base.org
ETHEREUM_RPC_URL=https://eth.llamarpc.com
ARBITRUM_RPC_URL=https://arb1.arbitrum.io/rpc
```

## Production Considerations

- Set `APP_ENV=production` to disable Uvicorn auto-reload.
- Use dedicated RPC endpoints (e.g. Alchemy, Infura) instead of public defaults for reliable finality checks.
- Configure `GOLDSKY_API_KEY` and `GOLDSKY_PROJECT_ID` to enable live blockchain event indexing.
- Store secrets (`SUPABASE_SERVICE_ROLE_KEY`, `SVIX_API_KEY`) in a secrets manager rather than `.env` files.
- Set `LOG_LEVEL=warning` or `error` to reduce log volume.
