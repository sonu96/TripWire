"""Goldsky Turbo pipeline configuration for ERC-3009 event ingestion.

ERC-3009's transferWithAuthorization is a *function*, not an event. The actual
events emitted on-chain are:
  1. ERC-20 Transfer(address indexed from, address indexed to, uint256 value)
  2. AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce)

We filter for AuthorizationUsed on USDC contracts to specifically track
x402/ERC-3009 payments (vs regular transfers). The Goldsky pipeline uses a
SQL transform to decode the logs and delivers decoded rows via webhook.
"""

import subprocess
import tempfile
from pathlib import Path

import structlog
import yaml

from tripwire.config.settings import settings
from tripwire.ingestion.decoder import AUTHORIZATION_USED_TOPIC, TRANSFER_TOPIC
from tripwire.types.models import CHAIN_NAMES, USDC_CONTRACTS, ChainId

logger = structlog.get_logger(__name__)

# ABI fragments for _gs_log_decode
_AUTHORIZATION_USED_ABI = (
    "event AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce)"
)
_TRANSFER_ABI = (
    "event Transfer(address indexed from, address indexed to, uint256 value)"
)

# Goldsky dataset names per chain
_DATASET_NAMES: dict[ChainId, str] = {
    ChainId.ETHEREUM: "ethereum.raw_logs",
    ChainId.BASE: "base.raw_logs",
    ChainId.ARBITRUM: "arbitrum.raw_logs",
}


def build_pipeline_config(chain_id: ChainId) -> dict:
    """Build a Goldsky Turbo pipeline config for a given chain.

    The pipeline uses the Goldsky dataset source (raw_logs) with a SQL
    transform that filters for AuthorizationUsed events on the USDC contract
    and decodes them via _gs_log_decode. Results are delivered via webhook
    to the TripWire ingestion endpoint.
    """
    chain_name = CHAIN_NAMES[chain_id]
    usdc_address = USDC_CONTRACTS[chain_id].lower()
    dataset = _DATASET_NAMES[chain_id]

    # SQL transform: JOIN AuthorizationUsed and Transfer events from the same
    # transaction on the same USDC contract. This gives us both the
    # authorizer/nonce (from AuthorizationUsed) and from/to/value (from
    # Transfer) in a single row, enabling endpoint matching by to_address.
    transform_sql = (
        f"SELECT "
        f"auth.id, "
        f"auth.block_number, "
        f"auth.block_hash, "
        f"auth.transaction_hash, "
        f"auth.log_index, "
        f"auth.block_timestamp, "
        f"auth.address, "
        f"{chain_id.value} AS chain_id, "
        f"_gs_log_decode('{_AUTHORIZATION_USED_ABI}', auth.topics, auth.data) AS decoded, "
        f"_gs_log_decode('{_TRANSFER_ABI}', xfer.topics, xfer.data).\"from\" AS from_address, "
        f"_gs_log_decode('{_TRANSFER_ABI}', xfer.topics, xfer.data).to AS to_address, "
        f"_gs_log_decode('{_TRANSFER_ABI}', xfer.topics, xfer.data) AS transfer "
        f"FROM {chain_name}_logs AS auth "
        f"INNER JOIN {chain_name}_logs AS xfer "
        f"ON auth.transaction_hash = xfer.transaction_hash "
        f"AND auth.address = xfer.address "
        f"WHERE auth.address = '{usdc_address}' "
        f"AND auth.topic0 = '{AUTHORIZATION_USED_TOPIC}' "
        f"AND xfer.topic0 = '{TRANSFER_TOPIC}'"
    )

    return {
        "version": "1",
        "name": f"tripwire-{chain_name}-erc3009",
        "sources": {
            f"{chain_name}_logs": {
                "type": "dataset",
                "dataset_name": dataset,
                "version": "1.0.0",
            }
        },
        "transforms": {
            "erc3009_decoded": {
                "primary_key": "id",
                "sql": transform_sql,
            }
        },
        "sinks": {
            "tripwire_webhook": {
                "type": "webhook",
                "from": "erc3009_decoded",
                "url": f"{settings.app_base_url}/api/v1/ingest/goldsky",
                "one_row_per_request": True,
                "headers": {
                    "Authorization": f"Bearer {settings.goldsky_webhook_secret.get_secret_value()}",
                    "Content-Type": "application/json",
                },
            }
        },
    }


def build_pipeline_yaml(chain_id: ChainId) -> str:
    """Return the pipeline config as a YAML string."""
    config = build_pipeline_config(chain_id)
    return yaml.dump(config, default_flow_style=False, sort_keys=False)


def build_all_pipeline_configs() -> dict[ChainId, dict]:
    """Build pipeline configs for all supported chains."""
    return {chain_id: build_pipeline_config(chain_id) for chain_id in ChainId}


# ── Pipeline lifecycle helpers ────────────────────────────────


def _run_goldsky(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a goldsky CLI command."""
    cmd = ["goldsky", *args]
    if settings.goldsky_api_key.get_secret_value():
        cmd.extend(["--api-key", settings.goldsky_api_key.get_secret_value()])
    if settings.goldsky_project_id:
        cmd.extend(["--project-id", settings.goldsky_project_id])

    logger.info("goldsky_cli", command=" ".join(cmd[:3]))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def deploy_pipeline(chain_id: ChainId) -> str:
    """Deploy a pipeline via the Goldsky CLI.

    Returns the CLI stdout on success, raises on failure.
    """
    chain_name = CHAIN_NAMES[chain_id]
    config_yaml = build_pipeline_yaml(chain_id)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix=f"tripwire-{chain_name}-", delete=False
    ) as f:
        f.write(config_yaml)
        config_path = f.name

    try:
        result = _run_goldsky(["turbo", "apply", config_path])
        if result.returncode != 0:
            logger.error(
                "goldsky_deploy_failed",
                chain=chain_name,
                stderr=result.stderr,
            )
            raise RuntimeError(f"Goldsky deploy failed: {result.stderr}")
        logger.info("goldsky_deploy_ok", chain=chain_name)
        return result.stdout
    finally:
        Path(config_path).unlink(missing_ok=True)


def get_pipeline_status(chain_id: ChainId) -> str:
    """Get the status of a deployed pipeline."""
    chain_name = CHAIN_NAMES[chain_id]
    pipeline_name = f"tripwire-{chain_name}-erc3009"
    result = _run_goldsky(["turbo", "status", pipeline_name])
    return result.stdout if result.returncode == 0 else result.stderr


def stop_pipeline(chain_id: ChainId) -> str:
    """Stop a deployed pipeline."""
    chain_name = CHAIN_NAMES[chain_id]
    pipeline_name = f"tripwire-{chain_name}-erc3009"
    result = _run_goldsky(["turbo", "stop", pipeline_name])
    if result.returncode != 0:
        logger.error("goldsky_stop_failed", chain=chain_name, stderr=result.stderr)
        raise RuntimeError(f"Goldsky stop failed: {result.stderr}")
    logger.info("goldsky_stop_ok", chain=chain_name)
    return result.stdout


def start_pipeline(chain_id: ChainId) -> str:
    """Start an existing (stopped) pipeline."""
    chain_name = CHAIN_NAMES[chain_id]
    pipeline_name = f"tripwire-{chain_name}-erc3009"
    result = _run_goldsky(["turbo", "start", pipeline_name])
    if result.returncode != 0:
        logger.error("goldsky_start_failed", chain=chain_name, stderr=result.stderr)
        raise RuntimeError(f"Goldsky start failed: {result.stderr}")
    logger.info("goldsky_start_ok", chain=chain_name)
    return result.stdout
