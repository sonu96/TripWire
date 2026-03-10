"""Webhook signature verification for TripWire SDK consumers.

Works standalone -- only requires the ``svix`` package (install with
``pip install tripwire-sdk[webhook]``).
"""

from __future__ import annotations

from svix.webhooks import Webhook, WebhookVerificationError


def verify_webhook_signature(
    payload: str | bytes,
    headers: dict[str, str],
    secret: str,
) -> bool:
    """Verify an incoming TripWire webhook signature.

    Args:
        payload: The raw request body (string or bytes).
        headers: The request headers dict. Must contain
            ``svix-id``, ``svix-timestamp``, and ``svix-signature``.
        secret: The endpoint signing secret (``whsec_...``).

    Returns:
        True if the signature is valid, False otherwise.
    """
    try:
        wh = Webhook(secret)
        wh.verify(payload, headers)
        return True
    except WebhookVerificationError:
        return False
