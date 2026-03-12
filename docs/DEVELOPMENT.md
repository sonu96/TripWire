# TripWire Development Guide

## Setup

### Clone and Install

```bash
git clone <repo-url> && cd TripWire

# Create a virtual environment (Python 3.11+)
python3.11 -m venv .venv
source .venv/bin/activate

# Install TripWire and all dependencies
pip install -e ".[dev]"
```

The `.[dev]` extra installs test and lint tooling: `pytest`, `pytest-asyncio`, `pytest-httpx`, `ruff`, and `mypy`.

### SDK Development

TripWire includes an SDK (built via Hatch). Installing with `pip install -e .` puts the package in editable/dev mode so changes to `tripwire/` are reflected immediately without reinstalling.

### Environment File

Copy `.env.example` (or create `.env`) with at minimum:

```bash
APP_ENV=development
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
```

Setting `APP_ENV=development` disables the production secret validator, so you can omit `CONVOY_API_KEY` and `TRIPWIRE_TREASURY_ADDRESS` during local development.

---

## Running Locally

### 1. Start Infrastructure

Use docker-compose to bring up Convoy and Redis:

```bash
docker compose up convoy-server convoy-worker convoy-postgres convoy-redis
```

This gives you:
- Convoy server at `localhost:5005`
- Convoy Postgres at `localhost:5433`
- Redis at `localhost:6380`

### 2. Start TripWire

```bash
python dev_server.py
```

This starts TripWire on port 3402 with:
- **Hot reload** enabled (via `uvicorn --reload`)
- **Auth bypass** -- all requests are treated as coming from Hardhat account #0 (`0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266`)
- **LogOnly webhook provider** if `CONVOY_API_KEY` is not set (webhooks are logged but not delivered)

To use a different dev wallet:

```bash
DEV_WALLET_ADDRESS=0xYourAddress python dev_server.py
```

The auth bypass uses FastAPI's `dependency_overrides` mechanism to replace `require_wallet_auth` with a function that returns a hardcoded `WalletAuthContext`. This bypass exists only in `dev_server.py` and is never imported by the production entry point (`tripwire/main.py`).

---

## Testing

### Configuration

Tests use `pytest` with `pytest-asyncio` in auto mode. Configuration is in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = [
    "integration: tests that require external services (x402 facilitator, etc.)",
]
```

### Test Wallets

Tests use deterministic Hardhat default accounts (defined in `tests/_wallet_helpers.py`):

| Wallet | Private Key | Address |
|--------|-------------|---------|
| Primary (`TEST_PRIVATE_KEY`) | `0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80` | `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` |
| Secondary (`OTHER_PRIVATE_KEY`) | `0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d` | `0x70997970C51812dc3A010C7d01b50e0d17dc79C8` |

These are well-known Hardhat keys with no real funds. They are used for SIWE signature generation in tests.

### Auth in Tests

`tests/conftest.py` sets `APP_ENV=testing` before any imports, so the dev bypass is never active. Tests handle auth in two ways:

1. **Dependency override** (most common): Replace `require_wallet_auth` with a mock that returns a `WalletAuthContext` for the desired wallet. This is the same mechanism `dev_server.py` uses.

2. **Real SIWE headers**: Use `make_auth_headers(account, method=..., path=...)` from `tests/_wallet_helpers.py` to generate valid SIWE-signed headers. Pair this with a `MockRedis` instance (seed the nonce via `mock_redis.seed_nonce(nonce)`) to test the full auth flow.

### MockRedis

`tests/_wallet_helpers.py` provides a `MockRedis` class that mimics `redis.asyncio.Redis` for nonce management:

```python
mock_redis = MockRedis()
mock_redis.seed_nonce("some-nonce")  # Pre-seeds siwe:nonce:some-nonce = "1"
```

### Running Tests

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Skip integration tests (those requiring external services)
pytest -m "not integration"

# Run a specific test file
pytest tests/test_auth.py
```

---

## Adding New Routes

Follow the pattern established in `tripwire/api/routes/endpoints.py`:

### 1. Accept `WalletAuthContext` via Dependency Injection

Every authenticated route must declare `require_wallet_auth` as a dependency:

```python
from tripwire.api.auth import require_wallet_auth, WalletAuthContext

@router.get("/my-resource/{resource_id}")
async def get_resource(
    resource_id: str,
    request: Request,
    wallet_auth: WalletAuthContext = Depends(require_wallet_auth),
):
    ...
```

### 2. Verify Ownership

After fetching a resource from the database, verify that the authenticated wallet owns it before returning data or allowing mutations:

```python
def _verify_ownership(row: dict, wallet_address: str) -> None:
    if row.get("owner_address", "").lower() != wallet_address.lower():
        raise HTTPException(status_code=403, detail="Not authorized")

# In the route handler:
row = supabase.table("endpoints").select("*").eq("id", endpoint_id).single().execute()
_verify_ownership(row.data, wallet_auth.wallet_address)
```

### 3. Use Structlog for Logging

```python
import structlog
logger = structlog.get_logger(__name__)

logger.info("resource_created", resource_id=resource_id, wallet=wallet_auth.wallet_address)
```

### 4. Apply Rate Limits

```python
from tripwire.api.ratelimit import CRUD_LIMIT, limiter

@router.post("/my-resource")
@limiter.limit(CRUD_LIMIT)
async def create_resource(...):
    ...
```

### 5. Register the Router

In `tripwire/main.py`, import and mount the router:

```python
from tripwire.api.routes.my_resource import router as my_resource_router
app.include_router(my_resource_router, prefix="/api/v1")
```

---

## Database Migrations

### Writing New Migrations

1. Create a new file in `tripwire/db/migrations/` with the next sequential number (e.g., `012_my_change.sql`).
2. Use `IF NOT EXISTS` / `IF EXISTS` guards so migrations are idempotent.
3. Add indexes for any columns used in WHERE clauses or JOINs.
4. If the new table needs multi-tenant isolation, add RLS policies following the pattern in `011_rls_policies.sql` -- join through `endpoints.owner_address` for child tables.

### Running Migrations

Migrations are plain SQL files. Run them against your Supabase database:

```bash
# Via Supabase Dashboard: SQL Editor > paste contents > Run
# Via psql:
psql "$SUPABASE_DB_URL" -f tripwire/db/migrations/012_my_change.sql
```

There is no automated migration runner. Always run migrations in numeric order. The full sequence is `001` through `011` (current).

---

## SDK Development

The SDK is built with Hatch. The version is read from `tripwire/__init__.py`:

```toml
[tool.hatch.version]
path = "tripwire/__init__.py"
```

### Local Development Workflow

1. Install in editable mode: `pip install -e ".[dev]"`
2. Make changes to any module under `tripwire/`.
3. Changes are reflected immediately (no reinstall needed).
4. Run tests: `pytest`
5. Lint: `ruff check .` and `ruff format .`
6. Type check: `mypy tripwire/`

### Building

```bash
pip install hatch
hatch build
```

---

## Code Style

### Linting and Formatting

TripWire uses `ruff` (configured in `pyproject.toml`):

- Target: Python 3.11
- Line length: 100
- Lint rules: `E` (pycodestyle errors), `F` (pyflakes), `I` (isort), `N` (naming), `W` (warnings)

```bash
ruff check .        # Lint
ruff format .       # Format
ruff check --fix .  # Auto-fix
```

### Logging

Always use `structlog`, never `print()` or `logging.getLogger()`:

```python
import structlog
logger = structlog.get_logger(__name__)

logger.info("event_name", key="value", count=42)
```

Log events should be `snake_case` strings. Include structured key-value context rather than formatting values into the message string.

### Pydantic Models

All request/response schemas and domain models use Pydantic v2 (`pydantic>=2.10.0`). Use `BaseModel` for data transfer objects and `BaseSettings` for configuration.

### EthAddress Type

Use the project's `EthAddress` type for Ethereum address fields. It handles checksumming and validation. Address comparisons should always be case-insensitive (`.lower()`).

### SecretStr for Secrets

All API keys, webhook secrets, and service role keys are typed as `pydantic.SecretStr` in settings. Access the value with `.get_secret_value()`. This prevents secrets from being logged or serialized accidentally.

---

## Security Checklist

Follow these rules for every change:

### Ownership Verification

- Every route that accesses user-scoped data MUST accept `WalletAuthContext` via `Depends(require_wallet_auth)`.
- After fetching any resource, verify `owner_address` matches the authenticated wallet before returning data or performing mutations.
- Use case-insensitive comparison (`.lower()`) for all address checks.

### Row Level Security

- All new tables that hold user-scoped data must have RLS enabled (`ALTER TABLE ... ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`).
- RLS policies should join through `endpoints.owner_address` for child tables, matching against `current_setting('app.current_wallet', true)`.
- Test that one wallet cannot access another wallet's data (use the `other_wallet` fixture in tests).

### No Dev Bypass in Auth Module

- The auth bypass (`dependency_overrides`) exists ONLY in `dev_server.py`.
- `tripwire/api/auth.py` must never contain dev bypass logic, hardcoded wallets, or conditional skips based on `APP_ENV`.
- The production entry point (`tripwire/main.py`) does not import or reference `dev_server.py`.

### Fail-Secure Defaults

- If `GOLDSKY_WEBHOOK_SECRET` is empty in non-development environments, ingest endpoints reject all requests (logged as a warning at startup).
- If `CONVOY_API_KEY` is empty, the webhook provider falls back to `LogOnlyProvider` (webhooks are logged but never delivered).
- Production startup fails fast if required secrets (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `CONVOY_API_KEY`, `TRIPWIRE_TREASURY_ADDRESS`) are missing.
- SIWE nonces are single-use (consumed from Redis on verification). Replay attacks are rejected.
- Timestamp tolerance (`AUTH_TIMESTAMP_TOLERANCE_SECONDS`, default 300s) rejects stale signatures.

### Secrets Handling

- Never log `SecretStr` values. Use `.get_secret_value()` only where the raw value is needed (e.g., passing to an HTTP header).
- Never commit `.env` files or hardcoded secrets. The test suite uses dummy values set in `conftest.py`.
- The `x402_registration_price` and payment gating are disabled when `TRIPWIRE_TREASURY_ADDRESS` is empty, so dev/test environments do not require a funded wallet.
