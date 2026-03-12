# TripWire Security Model

This document describes how TripWire authenticates requests, prevents replay attacks, enforces resource ownership, and protects secrets.

---

## 1. Authentication Model

TripWire uses **SIWE (Sign-In with Ethereum, EIP-4361)** wallet signatures for authentication. There are no API keys, no sessions, no cookies, and no JWTs. Every authenticated request carries a fresh cryptographic proof that the caller controls a specific Ethereum wallet.

The authentication flow works as follows:

1. The client requests a one-time nonce from the server.
2. The client constructs a SIWE message that includes the nonce, the request method, path, and a hash of the request body.
3. The client signs the message with their Ethereum private key (EIP-191 `personal_sign`).
4. The server recovers the signer address from the signature and verifies it matches the claimed address.
5. The server atomically consumes the nonce from Redis to prevent replays.

This model means authentication is fully stateless on the server side -- there is no session store, no token database, and no key management burden for operators.

---

## 2. SIWE Message Format

Every signed message follows the EIP-4361 structure exactly:

```
{domain} wants you to sign in with your Ethereum account:
{address}

{method} {path} {body_sha256_hex}

URI: https://{domain}
Version: 1
Chain ID: 1
Nonce: {nonce}
Issued At: {issued_at}
Expiration Time: {expiration_time}
```

Field descriptions:

| Field | Value |
|---|---|
| `domain` | The SIWE domain, configured as `siwe_domain` in settings (default: `tripwire.dev`) |
| `address` | The caller's checksummed Ethereum address (`0x...`) |
| `method` | HTTP method (`GET`, `POST`, `PUT`, `DELETE`) |
| `path` | Request path (e.g., `/endpoints`) |
| `body_sha256_hex` | Hex-encoded SHA-256 hash of the raw request body |
| `nonce` | Server-issued cryptographic nonce from `GET /auth/nonce` |
| `issued_at` | ISO-8601 UTC timestamp when the message was constructed |
| `expiration_time` | ISO-8601 UTC timestamp when the signature expires |

The statement line (`{method} {path} {body_sha256_hex}`) binds the signature to a specific HTTP request, preventing a signature for one endpoint from being reused against another.

---

## 3. Request Signing Flow

### Step 1: Obtain a nonce

```
GET /auth/nonce
```

Response:

```json
{"nonce": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"}
```

The nonce is stored in Redis with a 5-minute TTL (`_NONCE_TTL_SECONDS = 300`). This endpoint is rate-limited to 30 requests per minute.

### Step 2: Construct the SIWE message

Build the message using the format from Section 2 above. The statement is constructed as:

```
{HTTP_METHOD} {PATH} {SHA256(body)}
```

For a `POST /endpoints` request with a JSON body, the statement might look like:

```
POST /endpoints a1b2c3d4e5f6...
```

### Step 3: Sign with your wallet

Sign the message using EIP-191 `personal_sign` (also known as `eth_sign` with the standard prefix). The SDK provides `sign_auth_message()` which handles this:

```python
from tripwire_sdk.signer import sign_auth_message

signature, issued_at, expiration_time = sign_auth_message(
    key_or_account=private_key,
    address=wallet_address,
    nonce=nonce,
    method="POST",
    path="/endpoints",
    body_bytes=body,
)
```

### Step 4: Send the request with auth headers

Attach all five authentication headers (see Section 4) and send the request.

The SDK provides a convenience function that returns the complete header dict:

```python
from tripwire_sdk.signer import make_auth_headers

headers = make_auth_headers(
    key_or_account=private_key,
    address=wallet_address,
    path="/endpoints",
    nonce=nonce,
    method="POST",
    body_bytes=body,
)
```

---

## 4. Header Specification

Every authenticated request must include all five headers:

| Header | Format | Description |
|---|---|---|
| `X-TripWire-Address` | `0x...` (40 hex chars) | The caller's Ethereum address |
| `X-TripWire-Signature` | `0x...` (hex) | EIP-191 `personal_sign` signature of the SIWE message |
| `X-TripWire-Nonce` | URL-safe base64 string | Server-issued nonce from `GET /auth/nonce` |
| `X-TripWire-Issued-At` | ISO-8601 UTC | Timestamp when the message was signed |
| `X-TripWire-Expiration` | ISO-8601 UTC | Expiration timestamp for the signature |

If any header is missing, the server returns `401` with the message:

```
Missing authentication headers; X-TripWire-Address, X-TripWire-Signature,
X-TripWire-Nonce, X-TripWire-Issued-At, and X-TripWire-Expiration are all required
```

---

## 5. Replay Prevention

Replay attacks are prevented through server-issued nonces stored in Redis:

1. **Nonce generation**: `GET /auth/nonce` creates a cryptographically random 32-byte URL-safe token via `secrets.token_urlsafe(32)`.
2. **Redis storage**: The nonce is stored as `siwe:nonce:{nonce}` with a 5-minute TTL (`SETEX`).
3. **Atomic consumption**: During authentication, the server calls `DELETE siwe:nonce:{nonce}` on Redis. The `DELETE` command returns `1` if the key existed (valid nonce) or `0` if it did not (already consumed or expired). This is atomic -- concurrent requests with the same nonce will race on the `DELETE`, and only one will succeed.
4. **Expiration enforcement**: Even if a nonce has not been consumed, the server checks the `Expiration Time` field in the SIWE message and rejects expired signatures.

This two-layer approach (TTL expiry + atomic consumption) means each nonce can be used exactly once within a 5-minute window.

---

## 6. Body Integrity

The request body is bound to the signature through a SHA-256 hash:

1. The client computes `SHA256(raw_body_bytes)` and includes the hex digest in the SIWE statement line.
2. The server independently computes `SHA256(request.body())` and reconstructs the same SIWE message.
3. If the body was modified in transit, the reconstructed message will differ from the signed message, and signature recovery will yield a different address -- causing authentication to fail.

This guarantees that the request body cannot be tampered with after signing. For requests with no body (e.g., `GET`), the hash is the SHA-256 of an empty byte string (`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`).

---

## 7. Ownership Enforcement

Beyond verifying that a request is signed by a valid wallet, every route enforces that the wallet actually owns the resource being accessed. The authenticated wallet address from the `WalletAuthContext` is compared against the `owner_address` field on the resource (endpoint, subscription, etc.).

This is enforced at two layers:

- **Application layer**: Route handlers check `wallet.wallet_address` against the resource's `owner_address` before performing any operation.
- **Database layer**: Supabase RLS policies (see Section 8) ensure that even if an application-level check is missed, the database will not return rows belonging to another wallet.

---

## 8. Row Level Security

TripWire uses Supabase Row Level Security (RLS) as a defense-in-depth measure. RLS is enabled and forced on all tenant tables:

```sql
ALTER TABLE endpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE endpoints FORCE ROW LEVEL SECURITY;
```

`FORCE ROW LEVEL SECURITY` ensures that even the table owner (superuser) is subject to policies, preventing privilege escalation.

### Wallet context

Before each request, the application sets a session variable via a `SECURITY DEFINER` function:

```sql
CREATE OR REPLACE FUNCTION set_wallet_context(wallet_address text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    PERFORM set_config('app.current_wallet', wallet_address, true);
END;
$$;
```

The `true` parameter to `set_config` scopes the variable to the current transaction, preventing leakage across requests.

### Policies

Each table has an isolation policy that restricts access to rows owned by the current wallet:

- **`endpoints`**: Direct match on `lower(owner_address) = lower(current_setting('app.current_wallet', true))`.
- **`subscriptions`**, **`events`**, **`webhook_deliveries`**: Join through `endpoints` to verify ownership via `EXISTS (SELECT 1 FROM endpoints e WHERE e.id = {table}.endpoint_id AND lower(e.owner_address) = lower(current_setting('app.current_wallet', true)))`.

All comparisons use `lower()` to ensure case-insensitive matching of Ethereum addresses (EIP-55 checksummed vs. lowercase).

---

## 9. Secret Management

TripWire uses Pydantic's `SecretStr` type for all sensitive configuration values. `SecretStr` prevents secrets from appearing in logs, repr output, or serialized settings:

```python
supabase_service_role_key: SecretStr = SecretStr("")
convoy_api_key: SecretStr = SecretStr("")
webhook_signing_secret: SecretStr = SecretStr("")
goldsky_api_key: SecretStr = SecretStr("")
goldsky_webhook_secret: SecretStr = SecretStr("")
facilitator_webhook_secret: SecretStr = SecretStr("")
```

### Production validation

A `model_validator` on the `Settings` class enforces that critical secrets are set when `app_env == "production"`:

```python
@model_validator(mode="after")
def _validate_production_secrets(self) -> "Settings":
    if self.app_env != "production":
        return self

    missing: list[str] = []
    if not self.supabase_url:
        missing.append("supabase_url")
    if not self.supabase_service_role_key.get_secret_value():
        missing.append("supabase_service_role_key")
    if not self.convoy_api_key.get_secret_value():
        missing.append("convoy_api_key")
    if not self.tripwire_treasury_address:
        missing.append("tripwire_treasury_address")

    if missing:
        raise ValueError(
            f"Production environment requires the following settings: {', '.join(missing)}"
        )
    return self
```

The server will refuse to start in production if any required secret is missing or empty. Default values for secrets are empty strings, which means no secret is ever accidentally baked into the codebase.

---

## 10. Dev Mode

Authentication can be bypassed **only** through `dev_server.py`, which is a standalone entry point that is never imported by production code:

```python
# dev_server.py
os.environ.setdefault("APP_ENV", "development")

async def _dev_require_wallet_auth() -> WalletAuthContext:
    return WalletAuthContext(wallet_address=DEV_WALLET_ADDRESS)

app.dependency_overrides[require_wallet_auth] = _dev_require_wallet_auth
```

Key properties:

- The bypass uses FastAPI's `dependency_overrides` mechanism, which replaces the `require_wallet_auth` dependency at the framework level. The production auth code is never modified.
- The dev wallet defaults to `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` (Hardhat account #0) and can be overridden via the `DEV_WALLET_ADDRESS` environment variable.
- `APP_ENV` is forced to `"development"` before settings load, so the production validator in `Settings` will not run.
- The file prints a prominent banner on startup warning that auth is bypassed.

There is no `if dev_mode: skip_auth` conditional anywhere in the production codebase. The bypass exists exclusively in `dev_server.py`.

---

## 11. x402 Payment Security

TripWire uses the x402 protocol for payment-gated endpoint registration. Security considerations:

### Facilitator verification

The x402 facilitator (`x402_facilitator_url`, default: `https://x402.org/facilitator`) is responsible for verifying payment proofs. TripWire validates the facilitator's webhook callbacks using `facilitator_webhook_secret` (HMAC verification).

### ERC-3009 signatures

Payments use ERC-3009 `TransferWithAuthorization`, which requires:

- **`from_address`**: The payer's address (must match the `authorizer` field).
- **`valid_after` / `valid_before`**: Time bounds that constrain when the authorization can be executed.
- **`nonce`**: A unique bytes32 nonce preventing replay of the same authorization.

The `ERC3009Transfer` model enforces that the `authorizer` matches the `from_address` and that the `token` is a known USDC contract on a supported chain (`ChainId.ETHEREUM`, `ChainId.BASE`, or `ChainId.ARBITRUM`).

### Payment network

Registration payments are configured for Base mainnet (`eip155:8453`) by default, with USDC routed to the `tripwire_treasury_address`. The treasury address is a required secret in production and must be set before the server will start.
