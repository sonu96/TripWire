"""Policy evaluation engine for TripWire."""

from tripwire.types.models import AgentIdentity, EndpointPolicies, TransferData


def evaluate_policy(
    transfer: TransferData,
    identity: AgentIdentity | None,
    policies: EndpointPolicies,
) -> tuple[bool, str | None]:
    """Evaluate transfer against endpoint policies.

    Returns (allowed, reason). If allowed is False, reason explains why.
    """
    # Amount range checks
    amount = int(transfer.amount)

    if policies.min_amount is not None and amount < int(policies.min_amount):
        return False, f"Amount {amount} below minimum {policies.min_amount}"

    if policies.max_amount is not None and amount > int(policies.max_amount):
        return False, f"Amount {amount} above maximum {policies.max_amount}"

    # Sender blocklist
    sender = transfer.from_address.lower()

    if policies.blocked_senders:
        blocked = [s.lower() for s in policies.blocked_senders]
        if sender in blocked:
            return False, f"Sender {sender} is blocked"

    # Sender allowlist (if set, only listed senders are allowed)
    if policies.allowed_senders:
        allowed = [s.lower() for s in policies.allowed_senders]
        if sender not in allowed:
            return False, f"Sender {sender} not in allowlist"

    # Agent class check (requires identity)
    if policies.required_agent_class is not None:
        if identity is None:
            return False, "Agent identity required but not available"
        if identity.agent_class != policies.required_agent_class:
            return (
                False,
                f"Agent class '{identity.agent_class}' does not match "
                f"required '{policies.required_agent_class}'",
            )

    # Reputation score check (requires identity)
    if policies.min_reputation_score is not None:
        if identity is None:
            return False, "Agent identity required for reputation check but not available"
        if identity.reputation_score < policies.min_reputation_score:
            return (
                False,
                f"Reputation score {identity.reputation_score} below "
                f"minimum {policies.min_reputation_score}",
            )

    return True, None
