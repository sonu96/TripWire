"""TripWire configuration via pydantic-settings."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    app_env: str = "development"
    app_port: int = 3402
    log_level: str = "info"

    # Supabase
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str

    # Svix (Webhook Delivery)
    svix_api_key: str = ""

    # Goldsky
    goldsky_api_key: str = ""
    goldsky_project_id: str = ""
    goldsky_webhook_secret: str = ""

    # Blockchain RPC
    base_rpc_url: str = "https://mainnet.base.org"
    ethereum_rpc_url: str = "https://eth.llamarpc.com"
    arbitrum_rpc_url: str = "https://arb1.arbitrum.io/rpc"

    # API key rotation
    key_rotation_grace_hours: int = 24

    # Identity resolver
    identity_cache_ttl: int = 300

    # ERC-8004 (CREATE2 — same address on all chains)
    erc8004_identity_registry: str = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
    erc8004_reputation_registry: str = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"

    # USDC Contracts
    usdc_base: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    usdc_ethereum: str = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    usdc_arbitrum: str = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()  # type: ignore[call-arg]
