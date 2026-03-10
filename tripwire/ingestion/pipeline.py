"""Goldsky Mirror pipeline configuration for ERC-3009 event ingestion.

ERC-3009's transferWithAuthorization is a *function*, not an event. The actual
events emitted on-chain are:
  1. ERC-20 Transfer(address indexed from, address indexed to, uint256 value)
  2. AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce)

We filter for AuthorizationUsed on USDC contracts to specifically track
x402/ERC-3009 payments (vs regular transfers). The Goldsky pipeline uses a
SQL transform to decode the logs and sinks decoded rows into Supabase.
"""

import subprocess
import tempfile
from pathlib import Path

import structlog
import yaml

from tripwire.config.settings import settings
from tripwire.types.models import CHAIN_NAMES, USDC_CONTRACTS, ChainId

logger = structlog.get_logger()

# keccak256("AuthorizationUsed(address,bytes32)")
AUTHORIZATION_USED_TOPIC = (
    "0x98de503528ee59b575ef0c0a2576a82497bfc029"
    "a5685b209e9ec333479b10a5"
)

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f1"
    "63c4a11628f55a4df523b3ef"
)

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
    """Build a Goldsky Mirror pipeline config for a given chain.

    The pipeline uses the Goldsky dataset source (raw_logs) with a SQL
    transform that filters for AuthorizationUsed events on the USDC contract
    and decodes them via _gs_log_decode. Results are sunk into Supabase
    PostgreSQL with built-in reorg handling.
    """
    chain_name = CHAIN_NAMES[chain_id]
    usdc_address = USDC_CONTRACTS[chain_id].lower()
    dataset = _DATASET_NAMES[chain_id]

    # SQL transform: filter by USDC contract + AuthorizationUsed topic,
    # then decode the log using Goldsky's built-in ABI decoder.
    transform_sql = (
        f"SELECT "
        f"id, "
        f"block_number, "
        f"block_hash, "
        f"transaction_hash, "
        f"log_index, "
        f"block_timestamp, "
        f"_gs_log_decode('{_AUTHORIZATION_USED_ABI}', topics, data) AS decoded "
        f"FROM {chain_name}_logs "
        f"WHERE address = '{usdc_address}' "
        f"AND topic0 = '{AUTHORIZATION_USED_TOPIC}'"
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
            "supabase_sink": {
                "type": "postgres",
                "table": "erc3009_events",
                "schema": "public",
                "secret_name": "SUPABASE_SECRET",
                "from": "erc3009_decoded",
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
    if settings.goldsky_api_key:
        cmd.extend(["--api-key", settings.goldsky_api_key])
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
        result = _run_goldsky(["pipeline", "apply", config_path])
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
    result = _run_goldsky(["pipeline", "status", pipeline_name])
    return result.stdout if result.returncode == 0 else result.stderr


def stop_pipeline(chain_id: ChainId) -> str:
    """Stop a deployed pipeline."""
    chain_name = CHAIN_NAMES[chain_id]
    pipeline_name = f"tripwire-{chain_name}-erc3009"
    result = _run_goldsky(["pipeline", "stop", pipeline_name])
    if result.returncode != 0:
        logger.error("goldsky_stop_failed", chain=chain_name, stderr=result.stderr)
        raise RuntimeError(f"Goldsky stop failed: {result.stderr}")
    logger.info("goldsky_stop_ok", chain=chain_name)
    return result.stdout


def start_pipeline(chain_id: ChainId) -> str:
    """Start an existing (stopped) pipeline."""
    chain_name = CHAIN_NAMES[chain_id]
    pipeline_name = f"tripwire-{chain_name}-erc3009"
    result = _run_goldsky(["pipeline", "start", pipeline_name])
    if result.returncode != 0:
        logger.error("goldsky_start_failed", chain=chain_name, stderr=result.stderr)
        raise RuntimeError(f"Goldsky start failed: {result.stderr}")
    logger.info("goldsky_start_ok", chain=chain_name)
    return result.stdout
