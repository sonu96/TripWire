"""WebSocket eth_subscribe for real-time ERC-3009 log detection.

Medium-speed fast path (~200-500ms) that subscribes to on-chain logs via
WebSocket ``eth_subscribe`` and feeds matching events directly into the
EventProcessor pipeline.  Runs as one asyncio task per chain alongside the
primary Goldsky ingestion path.

Reconnection strategy:
  - Exponential backoff on disconnect: 1s → 2s → 4s → … → 60s cap.
  - On reconnect: backfill missed blocks via ``eth_getLogs``.
  - Track last-seen block number per chain to avoid re-processing.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import structlog
import websockets
import websockets.exceptions

from tripwire.config.settings import settings
from tripwire.ingestion.decoder import AUTHORIZATION_USED_TOPIC
from tripwire.observability.health import health_registry
from tripwire.types.models import USDC_CONTRACTS, ChainId

logger = structlog.get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────

# Backoff parameters (seconds)
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 60.0
_BACKOFF_FACTOR = 2.0

# WebSocket ping / subscription timeout
_WS_PING_INTERVAL = 20
_WS_PING_TIMEOUT = 20
_SUBSCRIBE_TIMEOUT = 10.0

# Map chain → settings attribute name for WebSocket URL
_CHAIN_WS_SETTINGS: dict[ChainId, str] = {
    ChainId.ETHEREUM: "ethereum_ws_url",
    ChainId.BASE: "base_ws_url",
    ChainId.ARBITRUM: "arbitrum_ws_url",
}


# ── Helpers ──────────────────────────────────────────────────────

def _ws_url_for_chain(chain_id: ChainId) -> str:
    """Return the configured WebSocket URL for *chain_id*."""
    attr = _CHAIN_WS_SETTINGS[chain_id]
    return getattr(settings, attr, "")


def _raw_log_from_ws(params: dict[str, Any], chain_id: ChainId) -> dict[str, Any]:
    """Convert an ``eth_subscribe`` log notification into the raw-log dict
    format consumed by ``decode_erc3009_from_logs`` / ``EventProcessor``.

    The processor can handle raw ABI-encoded logs via
    ``decode_erc3009_from_logs`` — we just need to normalise field names from
    the JSON-RPC snake_case / camelCase variants.
    """
    result = params.get("result", {})
    return {
        "address": result.get("address", ""),
        "topics": result.get("topics", []),
        "data": result.get("data", "0x"),
        "blockNumber": result.get("blockNumber", "0x0"),
        "blockHash": result.get("blockHash", ""),
        "transactionHash": result.get("transactionHash", ""),
        "logIndex": result.get("logIndex", "0x0"),
        "timestamp": 0,  # not available in subscription notifications
        "chain_id": chain_id.value,
    }


def _build_subscribe_params(chain_id: ChainId) -> dict[str, Any]:
    """Build the JSON-RPC ``eth_subscribe`` request for AuthorizationUsed logs."""
    usdc_address = USDC_CONTRACTS.get(chain_id, "")
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_subscribe",
        "params": [
            "logs",
            {
                "address": usdc_address,
                "topics": [AUTHORIZATION_USED_TOPIC],
            },
        ],
    }


def _build_get_logs_params(
    chain_id: ChainId,
    from_block: int,
    to_block: str = "latest",
) -> dict[str, Any]:
    """Build a JSON-RPC ``eth_getLogs`` request for backfilling missed blocks."""
    usdc_address = USDC_CONTRACTS.get(chain_id, "")
    return {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "eth_getLogs",
        "params": [
            {
                "address": usdc_address,
                "topics": [AUTHORIZATION_USED_TOPIC],
                "fromBlock": hex(from_block),
                "toBlock": to_block,
            }
        ],
    }


def _block_number_from_hex(hex_str: str) -> int:
    """Convert a hex block number string to int."""
    if isinstance(hex_str, int):
        return hex_str
    return int(hex_str, 16) if hex_str else 0


# ── Per-chain subscriber ─────────────────────────────────────────


class ChainSubscriber:
    """Manages a single WebSocket subscription for one chain.

    Handles connection, subscription, message routing, reconnection with
    exponential backoff, and block-gap backfill.
    """

    def __init__(self, chain_id: ChainId, processor: Any) -> None:
        self.chain_id = chain_id
        self.processor = processor
        self._last_block: int = 0
        self._subscription_id: str | None = None
        self._running = False

    async def run(self) -> None:
        """Main loop — connect, subscribe, listen; reconnect on failure."""
        ws_url = _ws_url_for_chain(self.chain_id)
        if not ws_url:
            logger.warning(
                "ws_subscriber_no_url",
                chain_id=self.chain_id.value,
                msg="No WebSocket URL configured; subscriber will not start",
            )
            return

        self._running = True
        backoff = _BACKOFF_INITIAL

        while self._running:
            try:
                await self._connect_and_listen(ws_url)
                # Clean disconnect — reset backoff
                backoff = _BACKOFF_INITIAL
            except asyncio.CancelledError:
                logger.info("ws_subscriber_cancelled", chain_id=self.chain_id.value)
                return
            except Exception:
                logger.exception(
                    "ws_subscriber_error",
                    chain_id=self.chain_id.value,
                    backoff_s=backoff,
                )
                health_registry.record_error("ws_subscriber", f"error on chain {self.chain_id.value}")

            if not self._running:
                return

            logger.info(
                "ws_subscriber_reconnecting",
                chain_id=self.chain_id.value,
                backoff_s=backoff,
            )
            await asyncio.sleep(backoff)
            health_registry.record_run("ws_subscriber")
            backoff = min(backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)

    async def _connect_and_listen(self, ws_url: str) -> None:
        """Open a WS connection, subscribe, backfill if needed, then listen."""
        logger.info("ws_subscriber_connecting", chain_id=self.chain_id.value, url=ws_url)

        async with websockets.connect(
            ws_url,
            ping_interval=_WS_PING_INTERVAL,
            ping_timeout=_WS_PING_TIMEOUT,
        ) as ws:
            # Send eth_subscribe
            subscribe_msg = _build_subscribe_params(self.chain_id)
            await ws.send(json.dumps(subscribe_msg))

            # Wait for subscription confirmation
            raw_resp = await asyncio.wait_for(ws.recv(), timeout=_SUBSCRIBE_TIMEOUT)
            resp = json.loads(raw_resp)
            if "result" in resp:
                self._subscription_id = resp["result"]
                logger.info(
                    "ws_subscriber_subscribed",
                    chain_id=self.chain_id.value,
                    subscription_id=self._subscription_id,
                )
            else:
                error = resp.get("error", {})
                logger.error(
                    "ws_subscriber_subscribe_failed",
                    chain_id=self.chain_id.value,
                    error=error,
                )
                return

            # Connection established and subscribed — record liveness
            health_registry.record_run("ws_subscriber")

            # Backfill missed blocks if we have a last-known block
            if self._last_block > 0:
                await self._backfill(ws)

            # Listen for incoming log notifications
            async for message in ws:
                if not self._running:
                    break
                await self._handle_message(message)
                health_registry.record_run("ws_subscriber")

    async def _backfill(self, ws: Any) -> None:
        """Fetch missed logs since last seen block via eth_getLogs."""
        from_block = self._last_block + 1
        logger.info(
            "ws_subscriber_backfilling",
            chain_id=self.chain_id.value,
            from_block=from_block,
        )

        get_logs_msg = _build_get_logs_params(self.chain_id, from_block)
        await ws.send(json.dumps(get_logs_msg))

        raw_resp = await asyncio.wait_for(ws.recv(), timeout=30.0)
        resp = json.loads(raw_resp)

        # The response might be a subscription notification rather than our
        # getLogs response (race condition). We handle both.
        if resp.get("id") == 2 and "result" in resp:
            logs = resp["result"]
            if logs:
                logger.info(
                    "ws_subscriber_backfill_received",
                    chain_id=self.chain_id.value,
                    log_count=len(logs),
                )
                for log_entry in logs:
                    await self._process_raw_log(log_entry)
        elif "params" in resp:
            # Got a subscription notification before our getLogs response;
            # process it normally — backfill response will arrive next.
            await self._handle_subscription_notification(resp)

    async def _handle_message(self, raw_message: str | bytes) -> None:
        """Route an incoming WebSocket message."""
        try:
            msg = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("ws_subscriber_invalid_json", chain_id=self.chain_id.value)
            return

        if "params" in msg:
            await self._handle_subscription_notification(msg)
        # Ignore other message types (e.g. responses to our own requests)

    async def _handle_subscription_notification(self, msg: dict[str, Any]) -> None:
        """Process an ``eth_subscription`` notification."""
        params = msg.get("params", {})
        subscription = params.get("subscription")

        if subscription != self._subscription_id:
            return

        result = params.get("result", {})
        block_hex = result.get("blockNumber", "0x0")
        block_number = _block_number_from_hex(block_hex)

        # Update last seen block
        if block_number > self._last_block:
            self._last_block = block_number

        await self._process_raw_log(result)

    async def _process_raw_log(self, log_entry: dict[str, Any]) -> None:
        """Convert a raw log from the RPC response and feed to the processor.

        The processor's ``process_event`` expects the Goldsky-decoded format.
        For raw RPC logs we need to use ``decode_erc3009_from_logs`` which
        requires both a Transfer + AuthorizationUsed log from the same tx.

        Since we only subscribe to AuthorizationUsed events, we fetch the
        full tx receipt to get the paired Transfer log before decoding.
        """
        from tripwire.ingestion.decoder import decode_erc3009_from_logs

        tx_hash = log_entry.get("transactionHash", "")
        block_hex = log_entry.get("blockNumber", "0x0")
        block_number = _block_number_from_hex(block_hex)

        if block_number > self._last_block:
            self._last_block = block_number

        if not tx_hash:
            logger.warning("ws_subscriber_no_tx_hash", chain_id=self.chain_id.value)
            return

        # Fetch the full transaction receipt to get all logs (Transfer + AuthorizationUsed)
        try:
            all_logs = await self._fetch_tx_logs(tx_hash)
        except Exception:
            logger.exception(
                "ws_subscriber_fetch_receipt_failed",
                chain_id=self.chain_id.value,
                tx_hash=tx_hash,
            )
            return

        # Decode the paired ERC-3009 events
        try:
            transfer = decode_erc3009_from_logs(all_logs, chain_id=self.chain_id)
        except ValueError as exc:
            logger.warning(
                "ws_subscriber_decode_failed",
                chain_id=self.chain_id.value,
                tx_hash=tx_hash,
                reason=str(exc),
            )
            return

        # Build a Goldsky-compatible raw_log dict so the processor can handle it
        raw_log: dict[str, Any] = {
            "transaction_hash": transfer.tx_hash,
            "block_number": transfer.block_number,
            "block_hash": transfer.block_hash,
            "log_index": transfer.log_index,
            "block_timestamp": transfer.timestamp,
            "address": transfer.token,
            "chain_id": self.chain_id.value,
            "decoded": {
                "authorizer": transfer.authorizer,
                "nonce": transfer.nonce,
            },
            "transfer": {
                "from_address": transfer.from_address,
                "to_address": transfer.to_address,
                "value": transfer.value,
            },
        }

        t0 = time.perf_counter()
        try:
            result = await self.processor.process_event(raw_log)
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
            logger.info(
                "ws_subscriber_event_processed",
                chain_id=self.chain_id.value,
                tx_hash=tx_hash,
                status=result.get("status"),
                elapsed_ms=elapsed_ms,
                source="ws_subscriber",
            )
        except Exception:
            logger.exception(
                "ws_subscriber_process_failed",
                chain_id=self.chain_id.value,
                tx_hash=tx_hash,
            )

    async def _fetch_tx_logs(self, tx_hash: str) -> list[dict[str, Any]]:
        """Fetch all logs from a transaction receipt via HTTP RPC.

        Uses the chain's HTTP RPC URL (not WebSocket) to avoid blocking the
        subscription connection.
        """
        import httpx

        rpc_url = _http_rpc_url_for_chain(self.chain_id)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_getTransactionReceipt",
                    "params": [tx_hash],
                },
            )
            resp.raise_for_status()
            data = resp.json()

        receipt = data.get("result")
        if receipt is None:
            raise ValueError(f"No receipt found for tx {tx_hash}")

        return receipt.get("logs", [])

    def stop(self) -> None:
        """Signal the subscriber to stop reconnecting."""
        self._running = False


def _http_rpc_url_for_chain(chain_id: ChainId) -> str:
    """Return the HTTP RPC URL for a chain (for receipt fetching)."""
    mapping: dict[ChainId, str] = {
        ChainId.ETHEREUM: settings.ethereum_rpc_url,
        ChainId.BASE: settings.base_rpc_url,
        ChainId.ARBITRUM: settings.arbitrum_rpc_url,
    }
    return mapping[chain_id]


# ── Manager (started from main.py lifespan) ──────────────────────


class WebSocketSubscriberManager:
    """Manages per-chain WebSocket subscriber tasks.

    Usage::

        manager = WebSocketSubscriberManager(processor)
        await manager.start()
        ...
        await manager.stop()
    """

    def __init__(self, processor: Any) -> None:
        self._processor = processor
        self._subscribers: list[ChainSubscriber] = []
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """Launch one asyncio task per configured chain."""
        health_registry.register("ws_subscriber")

        for chain_id in (ChainId.ETHEREUM, ChainId.BASE, ChainId.ARBITRUM):
            ws_url = _ws_url_for_chain(chain_id)
            if not ws_url:
                logger.info(
                    "ws_subscriber_skipped",
                    chain_id=chain_id.value,
                    reason="no_ws_url_configured",
                )
                continue

            subscriber = ChainSubscriber(chain_id, self._processor)
            self._subscribers.append(subscriber)

            task = asyncio.create_task(
                subscriber.run(),
                name=f"ws_subscriber_{chain_id.name.lower()}",
            )
            self._tasks.append(task)
            logger.info("ws_subscriber_task_created", chain_id=chain_id.value)

        if self._tasks:
            logger.info("ws_subscriber_manager_started", chain_count=len(self._tasks))
        else:
            logger.warning("ws_subscriber_manager_no_chains")

    async def stop(self) -> None:
        """Cancel all subscriber tasks and wait for them to finish."""
        for subscriber in self._subscribers:
            subscriber.stop()

        for task in self._tasks:
            task.cancel()

        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        self._subscribers.clear()
        self._tasks.clear()
        logger.info("ws_subscriber_manager_stopped")
