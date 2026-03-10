"""Tests for tripwire/webhook/dispatcher.py."""

from datetime import datetime, timezone

from tripwire.types.models import (
    ChainId,
    ERC3009Transfer,
    Endpoint,
    EndpointMode,
    EndpointPolicies,
    Subscription,
    SubscriptionFilter,
    AgentIdentity,
)
from tripwire.webhook.dispatcher import (
    build_transfer_data,
    match_endpoints,
    match_subscriptions,
)

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SENDER = "0x1234567890abcdef1234567890abcdef12345678"
RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
TX_HASH = "0x" + "ff" * 32
BLOCK_HASH = "0x" + "ee" * 32
NONCE_HEX = "0x" + "ab" * 32
NOW = datetime.now(timezone.utc)


def _transfer(
    to_address: str = RECIPIENT,
    from_address: str = SENDER,
    chain_id: ChainId = ChainId.BASE,
    value: str = "5000000",
) -> ERC3009Transfer:
    return ERC3009Transfer(
        chain_id=chain_id,
        tx_hash=TX_HASH,
        block_number=100,
        block_hash=BLOCK_HASH,
        log_index=3,
        from_address=from_address,
        to_address=to_address,
        value=value,
        authorizer=from_address,
        valid_after=0,
        valid_before=0,
        nonce=NONCE_HEX,
        token=USDC_BASE,
        timestamp=1700000000,
    )


def _endpoint(
    recipient: str = RECIPIENT,
    chains: list[int] | None = None,
    active: bool = True,
    mode: EndpointMode = EndpointMode.EXECUTE,
) -> Endpoint:
    return Endpoint(
        id="ep_test123",
        url="https://myapp.example.com/webhook",
        mode=mode,
        chains=chains or [8453],
        recipient=recipient,
        policies=EndpointPolicies(),
        active=active,
        created_at=NOW,
        updated_at=NOW,
    )


def _subscription(
    filters: SubscriptionFilter | None = None,
    active: bool = True,
) -> Subscription:
    return Subscription(
        id="sub_test123",
        endpoint_id="ep_test123",
        filters=filters or SubscriptionFilter(),
        active=active,
        created_at=NOW,
    )


def test_match_endpoints_by_recipient_and_chain():
    transfer = _transfer()
    endpoints = [_endpoint()]
    matched = match_endpoints(transfer, endpoints)
    assert len(matched) == 1
    assert matched[0].id == "ep_test123"


def test_match_endpoints_case_insensitive():
    transfer = _transfer(to_address=RECIPIENT.lower())
    ep = _endpoint(recipient=RECIPIENT.upper())
    matched = match_endpoints(transfer, [ep])
    assert len(matched) == 1


def test_match_endpoints_wrong_chain():
    transfer = _transfer(chain_id=ChainId.ETHEREUM)
    ep = _endpoint(chains=[8453])
    matched = match_endpoints(transfer, [ep])
    assert len(matched) == 0


def test_match_endpoints_inactive():
    transfer = _transfer()
    ep = _endpoint(active=False)
    matched = match_endpoints(transfer, [ep])
    assert len(matched) == 0


def test_match_subscriptions_basic():
    transfer = _transfer()
    sub = _subscription()
    matched = match_subscriptions(transfer, None, [sub])
    assert len(matched) == 1
    assert matched[0].id == "sub_test123"


def test_match_subscriptions_chain_filter():
    transfer = _transfer(chain_id=ChainId.BASE)
    sub_match = _subscription(filters=SubscriptionFilter(chains=[8453]))
    sub_no_match = _subscription(filters=SubscriptionFilter(chains=[1]))
    matched = match_subscriptions(transfer, None, [sub_match, sub_no_match])
    assert len(matched) == 1


def test_match_subscriptions_sender_filter():
    transfer = _transfer(from_address=SENDER)
    sub_match = _subscription(filters=SubscriptionFilter(senders=[SENDER]))
    other = "0x9999999999999999999999999999999999999999"
    sub_no_match = _subscription(filters=SubscriptionFilter(senders=[other]))
    matched = match_subscriptions(transfer, None, [sub_match, sub_no_match])
    assert len(matched) == 1


def test_build_transfer_data():
    transfer = _transfer(value="10000000")
    td = build_transfer_data(transfer)
    assert td.chain_id == ChainId.BASE
    assert td.tx_hash == TX_HASH
    assert td.block_number == 100
    assert td.from_address == SENDER
    assert td.to_address == RECIPIENT
    assert td.amount == "10000000"
    assert td.nonce == NONCE_HEX
    assert td.token == USDC_BASE
