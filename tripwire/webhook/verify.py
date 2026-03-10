"""Webhook signature verification for TripWire SDK consumers.

Wraps Svix's built-in HMAC verification so developers can verify
incoming webhooks with a single function call.
"""

from __future__ import annotations

import structlog
from svix.webhooks import Webhook, WebhookVerificationError

logger = structlog.get_logger(__name__)


def verify_webhook(
    payload: str | bytes,
    headers: dict[str, str],
    secret: str,
) -> bool:
    """Verify an incoming webhook's signature using Svix HMAC verification.

    Args:
        payload: The raw request body (string or bytes).
        headers: The request headers dict. Must contain:
            - svix-id
            - svix-timestamp
            - svix-signature
        secret: The endpoint signing secret (whsec_...).

    Returns:
        True if the signature is valid, False otherwise.
    """
    try:
        wh = Webhook(secret)
        wh.verify(payload, headers)
        return True
    except WebhookVerificationError:
        logger.warning("webhook_verification_failed")
        return False
    except Exception:
        logger.exception("webhook_verification_error")
        return False
