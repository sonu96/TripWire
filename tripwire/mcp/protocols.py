"""Pre-configured DeFi protocol pool data for the list_pools MCP tool.

Hardcoded for v1 — addresses are for Base mainnet.
Addresses marked with "0x..." are placeholders to be filled in later.
"""

from __future__ import annotations

from typing import Any

KNOWN_POOLS: dict[str, dict[str, Any]] = {
    "aerodrome": {
        "chain": "base",
        "type": "Slipstream (Concentrated Liquidity)",
        "pools": [
            {
                "name": "USDC/WETH",
                "address": "0x6cDcb1C4A4D1C3C6d054b27AC5B77e89eAFb971d",
                "tick_spacing": 100,
            },
            {
                "name": "USDC/USDT",
                "address": "0x0B25c51637c43decd6CC1C1e3da4518D54ddb528",  # Aerodrome USDC/USDT sAMM
                "tick_spacing": 1,
            },
            {
                "name": "WETH/AERO",
                "address": "0x7f670f78B17dEC44d5Ef68a48740b6f8849cc2e6",  # Aerodrome WETH/AERO vAMM
                "tick_spacing": 200,
            },
            {
                "name": "cbETH/WETH",
                "address": "0x44Ecc644449fC3a9858d2007CaA8CFAa4C561f91",  # Aerodrome cbETH/WETH
                "tick_spacing": 1,
            },
            {
                "name": "USDC/cbBTC",
                "address": "0x0c...",  # Placeholder — fill with actual address
                "tick_spacing": 200,
            },
        ],
        "events": ["Swap", "Mint", "Burn", "Collect"],
    },
    "aave-v3": {
        "chain": "base",
        "type": "Lending Protocol",
        "pools": [
            {
                "name": "Aave V3 Pool",
                "address": "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
            },
        ],
        "events": ["LiquidationCall", "Borrow", "Repay", "Supply", "Withdraw"],
    },
    "uniswap-v3": {
        "chain": "base",
        "type": "DEX (Concentrated Liquidity)",
        "pools": [
            {
                "name": "USDC/WETH 0.05%",
                "address": "0xd0b53D9277642d899DF5C87A3966A349A798F224",
                "fee": 500,
            },
            {
                "name": "USDC/WETH 0.3%",
                "address": "0x4C36388bE6F416A29C8d8Ae5C112AB4c1cAECf30",  # Uniswap V3 USDC/WETH 0.3%
                "fee": 3000,
            },
            {
                "name": "USDC/WETH 1%",
                "address": "0x0c...",  # Placeholder — fill with actual address
                "fee": 10000,
            },
        ],
        "events": ["Swap", "Mint", "Burn", "Collect"],
    },
}

# Chain name → list of supported protocols
CHAIN_PROTOCOLS: dict[str, list[str]] = {
    "base": ["aerodrome", "aave-v3", "uniswap-v3"],
    "ethereum": ["uniswap-v3", "aave-v3"],
    "arbitrum": ["uniswap-v3", "aave-v3"],
}
