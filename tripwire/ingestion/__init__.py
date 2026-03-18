"""Goldsky event ingestion pipeline for ERC-3009 transfers."""

from tripwire.ingestion.decoder import (
    decode_authorization_used,
    decode_erc3009_from_logs,
    decode_transfer_event,
    decode_transfer_log,
)
from tripwire.ingestion.finality import check_finality, check_finality_generic, get_block_number
from tripwire.ingestion.pipeline import (
    build_all_pipeline_configs,
    build_pipeline_config,
    build_pipeline_yaml,
    deploy_pipeline,
    get_pipeline_status,
    start_pipeline,
    stop_pipeline,
)

__all__ = [
    "build_all_pipeline_configs",
    "build_pipeline_config",
    "build_pipeline_yaml",
    "check_finality",
    "check_finality_generic",
    "decode_authorization_used",
    "decode_erc3009_from_logs",
    "decode_transfer_event",
    "decode_transfer_log",
    "deploy_pipeline",
    "get_block_number",
    "get_pipeline_status",
    "start_pipeline",
    "stop_pipeline",
]
