"""TripWire configuration via pydantic-settings."""

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_env: str = "production"
    app_port: int = 3402
    app_base_url: str = "http://localhost:3402"
    log_level: str = "info"
    cors_allowed_origins: list[str] = ["http://localhost:3000", "http://localhost:3402"]

    # Supabase
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: SecretStr = SecretStr("")

    # Convoy (Webhook Delivery)
    convoy_api_key: SecretStr = SecretStr("")
    convoy_url: str = "http://localhost:5005"
    webhook_signing_secret: SecretStr = SecretStr("")  # Default HMAC secret, can be overridden per endpoint

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

    # x402 Payment Gating
    x402_facilitator_url: str = "https://x402.org/facilitator"
    x402_registration_price: str = "$1.00"
    x402_network: str = "eip155:8453"  # Base mainnet

    # Wallet-based auth
    tripwire_treasury_address: str = ""  # USDC recipient for x402 registration payments
    auth_timestamp_tolerance_seconds: int = 300
    redis_url: str = "redis://localhost:6379"
    siwe_domain: str = "tripwire.dev"

    # Dead Letter Queue
    dlq_poll_interval_seconds: int = 60
    dlq_max_retries: int = 3
    dlq_alert_webhook_url: str = ""
    dlq_enabled: bool = True

    # Event bus
    event_bus_enabled: bool = False  # Enable Redis Streams event bus for async event processing
    event_bus_workers: int = 3  # Number of trigger worker processes for event bus

    # Finality poller
    finality_poller_enabled: bool = True
    finality_poll_interval_arbitrum: int = 5
    finality_poll_interval_base: int = 10
    finality_poll_interval_ethereum: int = 30

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
