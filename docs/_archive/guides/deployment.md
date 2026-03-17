# Deployment Guide

This guide covers deploying TripWire to production. TripWire depends on three external services -- Supabase (database), Convoy self-hosted (webhook delivery), and Goldsky (blockchain indexing) -- all of which are available at zero cost to get started.

## External Service Setup

### 1. Supabase Project

1. Create a project at [supabase.com](https://supabase.com).
2. Go to **Settings > API** and copy:
   - `SUPABASE_URL` (Project URL)
   - `SUPABASE_ANON_KEY` (anon/public key)
   - `SUPABASE_SERVICE_ROLE_KEY` (service_role key -- keep secret)
3. Go to **SQL Editor** and run the initial migration:

```sql
-- Paste contents of tripwire/db/migrations/001_initial.sql
```

This creates the `endpoints`, `subscriptions`, `events`, `nonces`, `webhook_deliveries`, and `audit_log` tables.

**Free tier includes**: 500 MB database, 50k monthly active users, unlimited API requests.

### 2. Convoy Self-Hosted

1. Deploy Convoy from [getconvoy.io](https://getconvoy.io).
2. Create a project in the dashboard.
3. Copy your **API Key** (`CONVOY_API_KEY`).

**Self-hosted includes**: Unlimited messages, retry logic, HMAC signing, delivery logs, dead letter queue.

### 3. Goldsky Pipelines

Follow the [Goldsky Setup Guide](./goldsky-setup.md) to deploy indexing pipelines for each chain you want to support.

**Free tier includes**: Turbo pipelines with real-time webhook streaming.

## Environment Variables

Create a `.env` file from the template:

```bash
cp .env.example .env
```

Required variables:

| Variable | Description |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_ANON_KEY` | Supabase anon/public key |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key |
| `CONVOY_API_KEY` | Convoy API key |
| `GOLDSKY_API_KEY` | Goldsky API key |
| `GOLDSKY_PROJECT_ID` | Goldsky project ID |
| `APP_ENV` | `production` for deployed environments |
| `APP_PORT` | Port to listen on (default: `3402`) |
| `BASE_RPC_URL` | Base RPC endpoint (default: `https://mainnet.base.org`) |
| `ETHEREUM_RPC_URL` | Ethereum RPC endpoint (default: `https://eth.llamarpc.com`) |
| `ARBITRUM_RPC_URL` | Arbitrum RPC endpoint (default: `https://arb1.arbitrum.io/rpc`) |

## Option A: Railway

[Railway](https://railway.app) offers one-click deploys from GitHub.

1. Push your code to a GitHub repository.
2. Go to [railway.app](https://railway.app) and create a new project.
3. Select **Deploy from GitHub repo** and connect your repository.
4. Railway auto-detects the `Dockerfile`. If not, set the builder to Dockerfile.
5. Add environment variables in **Settings > Variables** (paste from your `.env`).
6. Set the port to `3402` under **Settings > Networking > Public Networking**.
7. Deploy. Railway will build the Docker image and start the service.

**Health check**: Railway will monitor `GET /health` automatically once networking is configured.

**Custom domain**: Under **Settings > Networking**, add your custom domain and configure DNS.

## Option B: Fly.io

1. Install the Fly CLI:

```bash
curl -L https://fly.io/install.sh | sh
```

2. Authenticate:

```bash
fly auth login
```

3. Launch the app (from the project root):

```bash
fly launch --name tripwire --region iad --no-deploy
```

4. Set secrets:

```bash
fly secrets set \
  SUPABASE_URL="https://your-project.supabase.co" \
  SUPABASE_ANON_KEY="your_anon_key" \
  SUPABASE_SERVICE_ROLE_KEY="your_service_role_key" \
  CONVOY_API_KEY="your_convoy_api_key" \
  GOLDSKY_API_KEY="your_goldsky_api_key" \
  GOLDSKY_PROJECT_ID="your_project_id" \
  APP_ENV="production"
```

5. Update `fly.toml` to configure the health check and port:

```toml
[http_service]
  internal_port = 3402
  force_https = true

[[http_service.checks]]
  interval = "30s"
  timeout = "5s"
  grace_period = "10s"
  method = "GET"
  path = "/health"
```

6. Deploy:

```bash
fly deploy
```

7. Check status:

```bash
fly status
fly logs
```

## Option C: Docker on Any VPS

Works with any provider (DigitalOcean, Hetzner, AWS EC2, etc.).

1. SSH into your server and clone the repo:

```bash
git clone https://github.com/your-org/tripwire.git
cd tripwire
```

2. Create the `.env` file with your production values:

```bash
cp .env.example .env
# Edit .env with your actual values
```

3. Build and run with Docker:

```bash
docker build -t tripwire .
docker run -d \
  --name tripwire \
  --env-file .env \
  -e APP_ENV=production \
  -p 3402:3402 \
  --restart unless-stopped \
  tripwire
```

Or use Docker Compose:

```bash
docker compose up -d
```

4. Verify:

```bash
curl http://localhost:3402/health
# {"status":"ok"}
```

5. Set up a reverse proxy (nginx or Caddy) for HTTPS:

```
# Example Caddyfile
tripwire.yourdomain.com {
    reverse_proxy localhost:3402
}
```

## Health Check Monitoring

TripWire exposes `GET /health` which returns `{"status": "ok"}` when the service is running. Use this endpoint with:

- **Uptime monitoring**: UptimeRobot, Betterstack, or Checkly (all have free tiers).
- **Container orchestration**: Docker HEALTHCHECK (included in the Dockerfile), Kubernetes liveness probes, or Railway/Fly built-in checks.

## Scaling Considerations

TripWire is stateless -- all state lives in Supabase. This means you can scale horizontally by running multiple instances behind a load balancer.

- **Single instance** handles most workloads. Uvicorn's async architecture processes many concurrent requests on one process.
- **Multiple workers**: Add `--workers N` to the uvicorn command for CPU-bound workloads. Adjust the Dockerfile CMD or set the `WEB_CONCURRENCY` environment variable.
- **Horizontal scaling**: Run multiple containers behind a load balancer. No session affinity required since there is no local state.
- **Database**: Supabase free tier supports 500 MB and 60 connections. Upgrade to Pro ($25/mo) for 8 GB and 200 connections.
- **Webhooks**: Convoy self-hosted has no message limits. Scale by adding more Convoy instances as needed.

## Cost Breakdown

Starting from zero:

| Service | Free Tier | Paid Tier |
|---|---|---|
| **Supabase** | 500 MB database, 50k MAU | Pro: $25/mo (8 GB, 200 connections) |
| **Convoy** | Self-hosted (no limits) | Infrastructure cost only |
| **Goldsky** | Turbo pipelines | Growth: usage-based |
| **Hosting** | Railway free trial / Fly free tier | ~$5-10/mo for a small instance |
| **Total** | **$0 to start** | **~$30-110/mo at scale** |

Public RPC endpoints (Base, Ethereum, Arbitrum) are free. For production traffic, consider dedicated RPC providers like Alchemy or Infura.
