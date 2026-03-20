"""TripWire configuration via pydantic-settings."""

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings

from tripwire.types.models import ProductMode


class Settings(BaseSettings):
    # App
    app_env: str = "production"
    app_port: int = 3402
    app_base_url: str = "http://localhost:3402"
    log_level: str = "info"
    cors_allowed_origins: list[str] = ["http://localhost:3000", "http://localhost:3402"]

    # Product mode: "pulse" (generic triggers), "keeper" (x402 payments), or "both"
    product_mode: ProductMode = ProductMode.BOTH

    @property
    def is_pulse(self) -> bool:
        """True when running as Pulse (generic onchain triggers) or both."""
        return self.product_mode in (ProductMode.PULSE, ProductMode.BOTH)

    @property
    def is_keeper(self) -> bool:
        """True when running as Keeper (x402 payment webhooks) or both."""
        return self.product_mode in (ProductMode.KEEPER, ProductMode.BOTH)

    # Supabase
    supabase_url: str = ""
    supabase_anon_key: str = ""  # Unused — kept for .env compat; may be removed in future
    supabase_service_role_key: SecretStr = SecretStr("")

    # Direct Postgres (asyncpg — used for advisory locks / coordination only)
    database_url: str = ""

    # Convoy (Webhook Delivery)
    convoy_api_key: SecretStr = SecretStr("")
    convoy_url: str = "http://localhost:5005"
    webhook_signing_secret: SecretStr = SecretStr("")  # Unused — kept for .env compat; may be removed in future

    # Goldsky platform (CLI auth for deploying/managing Turbo pipelines)
    goldsky_api_key: SecretStr = SecretStr("")
    goldsky_project_id: str = ""
    goldsky_webhook_secret: SecretStr = SecretStr("")  # Validates inbound Goldsky Turbo webhooks

    # Goldsky Edge (managed RPC with caching + cross-node consensus)
    goldsky_edge_api_key: SecretStr = SecretStr("")

    # x402 Facilitator
    facilitator_webhook_secret: SecretStr = SecretStr("")

    # Blockchain RPC (Goldsky Edge endpoints)
    base_rpc_url: str = ""
    ethereum_rpc_url: str = ""
    arbitrum_rpc_url: str = ""

    # Block explorer API keys (for ABI fetching)
    basescan_api_key: str = ""
    etherscan_api_key: str = ""
    arbiscan_api_key: str = ""

    # x402 Payment Gating
    x402_facilitator_url: str = "https://x402.org/facilitator"
    x402_registration_price: str = "$1.00"
    x402_networks: list[str] = ["eip155:8453"]  # CAIP-2 chains (comma-separated in env)

    @field_validator("x402_networks", mode="before")
    @classmethod
    def _parse_x402_networks(cls, v: object) -> list[str]:
        """Accept both JSON arrays and comma-separated strings from env vars."""
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v  # type: ignore[return-value]

    @property
    def x402_primary_network(self) -> str:
        """Primary network for defaults."""
        return self.x402_networks[0] if self.x402_networks else "eip155:8453"

    @property
    def x402_network(self) -> str:
        """Deprecated: use x402_networks or x402_primary_network."""
        return self.x402_primary_network

    # Wallet-based auth
    tripwire_treasury_address: str = ""  # USDC recipient for x402 registration payments
    auth_timestamp_tolerance_seconds: int = 300
    redis_url: str = "redis://localhost:6379"
    siwe_domain: str = "tripwire.dev"
    siwe_chain_id: int = 8453

    # Dead Letter Queue
    dlq_poll_interval_seconds: int = 60
    dlq_max_retries: int = 3
    dlq_alert_webhook_url: str = ""
    dlq_enabled: bool = True

    # Event bus
    event_bus_enabled: bool = False  # Enable Redis Streams event bus for async event processing
    event_bus_workers: int = 3  # Number of trigger worker processes for event bus

    # Unified processor (C2) — single code path for ERC-3009 and dynamic triggers
    unified_processor: bool = False

    # Finality poller
    finality_poller_enabled: bool = True
    finality_poll_interval_arbitrum: int = 5
    finality_poll_interval_base: int = 10
    finality_poll_interval_ethereum: int = 30

    # Pre-confirmed event TTL (how long to wait for onchain confirmation)
    pre_confirmed_ttl_seconds: int = 1800  # 30 minutes
    pre_confirmed_sweep_interval_seconds: int = 300  # Check every 5 minutes

    # Identity resolver
    identity_cache_ttl: int = 300

    # ERC-8004 (CREATE2 — same address on all chains)
    erc8004_identity_registry: str = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
    erc8004_reputation_registry: str = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"

    # Metrics endpoint authentication (optional — set to protect /metrics in production)
    metrics_bearer_token: str = ""

    # Sentry (optional error tracking — install with: pip install tripwire[sentry])
    sentry_dsn: SecretStr = SecretStr("")
    sentry_traces_sample_rate: float = 0.1

    # OpenTelemetry (optional distributed tracing)
    otel_enabled: bool = False
    otel_endpoint: str = ""
    otel_service_name: str = "tripwire"

    # Resource quotas
    max_triggers_per_wallet: int = 50
    max_endpoints_per_wallet: int = 20

    # Session system (Keeper execution runtime)
    session_enabled: bool = False
    session_default_ttl_seconds: int = 900        # 15 min
    session_max_ttl_seconds: int = 1800           # 30 min
    session_default_budget_usdc: int = 10_000_000  # 10 USDC (6 decimals)
    session_max_budget_usdc: int = 100_000_000     # 100 USDC

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> "Settings":
        """Ensure critical secrets are set in production."""
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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()  # type: ignore[call-arg]
